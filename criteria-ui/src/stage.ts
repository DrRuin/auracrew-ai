import * as THREE from "three";
import type { PDFDocumentProxy } from "pdfjs-dist";
import { TIMING } from "./labels";
import { render } from "./pdf";

const PALETTE = { night: 0x141e4f, blue: 0x2b3f9e, ochre: 0xe0a43a, rose: 0xe27d6b, paper: 0xf2ecdf };
const still = matchMedia("(prefers-reduced-motion: reduce)");

type Pose = { position: THREE.Vector3; rotation: THREE.Euler; scale: number; opacity: number };

class Sheet {
  group = new THREE.Group();
  page: THREE.Mesh<THREE.PlaneGeometry, THREE.MeshBasicMaterial>;
  edge: THREE.Mesh<THREE.PlaneGeometry, THREE.MeshBasicMaterial>;
  rest!: Pose;
  target!: Pose;

  constructor(public number: number, aspect: number) {
    this.page = new THREE.Mesh(
      new THREE.PlaneGeometry(1, aspect),
      new THREE.MeshBasicMaterial({ color: PALETTE.paper, transparent: true, side: THREE.DoubleSide }),
    );
    this.edge = new THREE.Mesh(
      new THREE.PlaneGeometry(1.06, aspect + 0.06),
      new THREE.MeshBasicMaterial({ color: PALETTE.ochre, transparent: true, opacity: 0 }),
    );
    this.edge.position.z = -0.004;
    this.group.add(this.edge, this.page);
  }

  paint(canvas: HTMLCanvasElement) {
    const texture = new THREE.CanvasTexture(canvas);
    texture.colorSpace = THREE.SRGBColorSpace;
    texture.anisotropy = 4;
    this.page.material.map = texture;
    this.page.material.color.set(0xffffff);
    this.page.material.needsUpdate = true;
  }

  dispose() {
    for (const mesh of [this.page, this.edge]) {
      mesh.material.map?.dispose();
      mesh.material.dispose();
      mesh.geometry.dispose();
    }
  }
}

function facets(scene: THREE.Scene) {
  const colors = [PALETTE.blue, PALETTE.ochre, PALETTE.rose, PALETTE.blue, PALETTE.paper, PALETTE.blue, PALETTE.rose];
  return colors.map((color, i) => {
    const sides = i % 3 === 0 ? 4 : 3;
    const shape = new THREE.Shape();
    for (let k = 0; k < sides; k++) {
      const angle = (k / sides) * Math.PI * 2 + i;
      const radius = 1.8 + ((i * 7 + k * 3) % 5) * 0.45;
      const point = [Math.cos(angle) * radius, Math.sin(angle) * radius * 0.8] as const;
      if (k === 0) shape.moveTo(...point);
      else shape.lineTo(...point);
    }
    const mesh = new THREE.Mesh(
      new THREE.ShapeGeometry(shape),
      new THREE.MeshBasicMaterial({
        color,
        transparent: true,
        opacity: color === PALETTE.paper ? 0.1 : 0.5 + (i % 3) * 0.15,
        side: THREE.DoubleSide,
        depthWrite: false,
      }),
    );
    mesh.position.set(Math.sin(i * 2.1) * 3.6, Math.cos(i * 1.7) * 1.9, -3 - (i % 4) * 0.8);
    mesh.rotation.set(0, 0, i * 0.9);
    scene.add(mesh);
    return mesh;
  });
}

export class Stage {
  private renderer: THREE.WebGLRenderer;
  private scene = new THREE.Scene();
  private camera = new THREE.PerspectiveCamera(34, 1, 0.1, 50);
  private sheets: Sheet[] = [];
  private shards: THREE.Mesh[];
  private pointer = new THREE.Vector2();
  private busy = 0;
  private timer = new THREE.Timer();
  private build = 0;
  private span = 5;
  private row = new Map<number, Pose>();
  private saved: { pose: Pose; edge: number }[] | null = null;
  private caster = new THREE.Raycaster();
  private aim = new THREE.Vector3();
  private hover: { x: number; y: number } | null = null;
  private owned: PDFDocumentProxy | null = null;
  private dirty = true;
  pick?: (number: number) => void;

  constructor(private host: HTMLElement) {
    this.renderer = new THREE.WebGLRenderer({ antialias: true, alpha: true });
    this.renderer.setPixelRatio(Math.min(devicePixelRatio, 2));
    host.append(this.renderer.domElement);
    this.camera.position.set(0, 0, 7);
    this.scene.fog = new THREE.Fog(PALETTE.night, 10, 20);
    this.shards = facets(this.scene);
    new ResizeObserver(() => this.resize()).observe(host);
    host.addEventListener("pointermove", (event) => (this.hover = { x: event.clientX, y: event.clientY }));
    host.addEventListener("click", (event) => {
      const sheet = this.hit(event);
      if (sheet) this.pick?.(sheet.number);
    });
    this.timer.connect(document);
    this.renderer.setAnimationLoop((time) => this.frame(time));
  }

  async show(pdf: PDFDocumentProxy, hidden: boolean, painted?: (count: number, total: number) => void, owned = false) {
    const build = ++this.build;
    if (this.owned !== pdf) this.owned?.loadingTask.destroy();
    this.owned = owned ? pdf : null;
    this.dirty = true;
    this.sheets.forEach((sheet) => {
      this.scene.remove(sheet.group);
      sheet.dispose();
    });
    const first = await pdf.getPage(1);
    const size = first.getViewport({ scale: 1 });
    const aspect = size.height / size.width;
    this.sheets = Array.from({ length: pdf.numPages }, (_, i) => new Sheet(i + 1, aspect));
    this.sheets.forEach((sheet, i) => {
      sheet.rest = this.fan(i, this.sheets.length);
      sheet.target = this.away(sheet.rest);
      this.place(sheet, sheet.target);
      this.scene.add(sheet.group);
      if (!hidden) setTimeout(() => build === this.build && this.reveal(sheet.number), still.matches ? 0 : TIMING.reveal * i);
    });
    const width = pdf.numPages > 40 ? 320 : 640;
    for (const sheet of this.sheets) {
      const canvas = await render(pdf, sheet.number, width);
      if (build !== this.build) return;
      sheet.paint(canvas);
      this.dirty = true;
      painted?.(sheet.number, this.sheets.length);
    }
  }

  clear() {
    this.dirty = true;
    this.build++;
    this.owned?.loadingTask.destroy();
    this.owned = null;
    this.sheets.forEach((sheet) => {
      this.scene.remove(sheet.group);
      sheet.dispose();
    });
    this.sheets = [];
    this.row.clear();
    this.saved = null;
  }

  reveal(number: number) {
    this.dirty = true;
    const sheet = this.sheets[number - 1];
    if (sheet) sheet.target = sheet.rest;
  }

  working(on: boolean) {
    this.dirty = true;
    this.busy = on ? 1 : 0;
  }

  lift(pages: number[]) {
    this.dirty = true;
    this.row = this.line(pages, 0.45, 0.9, 0.92, 1, 0.95);
    for (const sheet of this.sheets) sheet.target = this.row.get(sheet.number) ?? this.back(sheet);
  }

  resolve(cited: number[]) {
    this.dirty = true;
    if (!cited.length) return;
    const chosen = new Set(cited);
    const front = this.line(cited, 0.4, 1.1, 0.95, 1, 1.05);
    const middle = this.line(
      [...this.row.keys()].filter((number) => !chosen.has(number)),
      1.15,
      -1,
      0.7,
      0.72,
      0.85,
    );
    this.saved = null;
    for (const sheet of this.sheets) {
      sheet.target = front.get(sheet.number) ?? middle.get(sheet.number) ?? this.back(sheet);
      sheet.edge.material.opacity = chosen.has(sheet.number) ? 1 : 0;
    }
  }

  cite(number: number) {
    this.dirty = true;
    const sheet = this.sheets[number - 1];
    if (!sheet) return;
    const pose = this.copy(this.row.get(number) ?? sheet.rest);
    pose.position.y += 0.3;
    pose.position.z += 0.35;
    pose.scale *= 1.08;
    pose.opacity = 1;
    sheet.target = pose;
    sheet.edge.material.opacity = 1;
  }

  focus(number: number | null) {
    this.dirty = true;
    if (number === null) {
      this.saved?.forEach(({ pose, edge }, i) => {
        const sheet = this.sheets[i];
        if (!sheet) return;
        sheet.target = pose;
        sheet.edge.material.opacity = edge;
      });
      this.saved = null;
      return;
    }
    this.saved ??= this.sheets.map((sheet) => ({ pose: sheet.target, edge: sheet.edge.material.opacity }));
    for (const sheet of this.sheets) {
      if (sheet.number === number) {
        sheet.target = {
          position: new THREE.Vector3(0, 0, 3.2),
          rotation: new THREE.Euler(0, 0, 0),
          scale: 1.15,
          opacity: 1,
        };
        sheet.edge.material.opacity = 1;
      } else sheet.target = { ...this.copy(sheet.target), opacity: 0.1 };
    }
  }

  settle() {
    this.dirty = true;
    this.row.clear();
    this.saved = null;
    for (const sheet of this.sheets) {
      sheet.target = sheet.rest;
      sheet.edge.material.opacity = 0;
    }
  }

  private line(pages: number[], y: number, z: number, scale: number, opacity: number, gap: number) {
    const spread = Math.min(pages.length * gap, this.span * 0.55);
    return new Map(
      pages.map((number, k): [number, Pose] => {
        const t = pages.length > 1 ? k / (pages.length - 1) - 0.5 : 0;
        return [
          number,
          {
            position: new THREE.Vector3(t * spread, y + Math.sin(k * 1.7) * 0.06, z - Math.abs(t) * 0.5),
            rotation: new THREE.Euler(0, -t * 0.4, -t * 0.06),
            scale,
            opacity,
          },
        ];
      }),
    );
  }

  private back(sheet: Sheet): Pose {
    const pose = this.copy(sheet.rest);
    pose.position.set(pose.position.x * 0.75, pose.position.y - 0.25, pose.position.z - 3.4);
    pose.scale = 0.85;
    pose.opacity = 0.2;
    return pose;
  }

  private hit(event: { clientX: number; clientY: number }): Sheet | null {
    const box = this.host.getBoundingClientRect();
    const at = new THREE.Vector2(((event.clientX - box.left) / box.width) * 2 - 1, -((event.clientY - box.top) / box.height) * 2 + 1);
    this.caster.setFromCamera(at, this.camera);
    const visible = this.sheets.filter((sheet) => sheet.page.material.opacity > 0.15);
    const [first] = this.caster.intersectObjects(visible.map((sheet) => sheet.page));
    return visible.find((sheet) => sheet.page === first?.object) ?? null;
  }

  private fan(i: number, n: number): Pose {
    const t = n > 1 ? i / (n - 1) - 0.5 : 0;
    const spread = Math.min(n * 0.5, this.span * 0.62);
    return {
      position: new THREE.Vector3(t * spread, Math.sin(i * 1.3) * 0.16 + 0.75, -Math.abs(t) * 1.6 + (i % 2) * 0.05),
      rotation: new THREE.Euler(Math.sin(i * 0.7) * 0.06, -t * 1.1, -t * 0.32 + Math.sin(i * 2.3) * 0.05),
      scale: 1.05,
      opacity: 1,
    };
  }

  private away(pose: Pose): Pose {
    const moved = this.copy(pose);
    moved.position.y -= 3.2;
    moved.position.z += 2.5;
    moved.rotation.x -= 1.1;
    moved.opacity = 0;
    return moved;
  }

  private copy(pose: Pose): Pose {
    return { ...pose, position: pose.position.clone(), rotation: pose.rotation.clone() };
  }

  private place(sheet: Sheet, pose: Pose) {
    sheet.group.position.copy(pose.position);
    sheet.group.rotation.copy(pose.rotation);
    sheet.group.scale.setScalar(pose.scale);
    sheet.page.material.opacity = pose.opacity;
  }

  private frame(now: number) {
    this.timer.update(now);
    const dt = Math.min(this.timer.getDelta(), 0.05);
    const time = this.timer.getElapsed();
    const ease = still.matches ? 1 : 1 - Math.exp(-dt * 5);
    if (this.hover) this.aimAt(this.hover);
    if (still.matches && !this.dirty) return;
    this.dirty = false;
    this.sheets.forEach((sheet, i) => {
      const { group, page } = sheet;
      const bob = still.matches ? 0 : Math.sin(time * 0.8 + i) * 0.03;
      group.position.lerp(this.aim.copy(sheet.target.position).setY(sheet.target.position.y + bob), ease);
      group.rotation.set(
        THREE.MathUtils.lerp(group.rotation.x, sheet.target.rotation.x, ease),
        THREE.MathUtils.lerp(group.rotation.y, sheet.target.rotation.y, ease),
        THREE.MathUtils.lerp(group.rotation.z, sheet.target.rotation.z, ease),
      );
      group.scale.setScalar(THREE.MathUtils.lerp(group.scale.x, sheet.target.scale, ease));
      page.material.opacity = THREE.MathUtils.lerp(page.material.opacity, sheet.target.opacity, ease);
      sheet.edge.visible = page.material.opacity > 0.5;
    });
    if (!still.matches) {
      this.shards.forEach((shard, i) => {
        shard.rotation.z += dt * (0.02 + this.busy * 0.25) * (i % 2 ? 1 : -1);
      });
      this.camera.position.x = THREE.MathUtils.lerp(this.camera.position.x, this.pointer.x * 1.2, ease * 0.4);
      this.camera.position.y = THREE.MathUtils.lerp(this.camera.position.y, -this.pointer.y * 0.7, ease * 0.4);
    }
    this.camera.lookAt(0, 0, 0);
    this.renderer.render(this.scene, this.camera);
  }

  private aimAt(at: { x: number; y: number }) {
    const box = this.host.getBoundingClientRect();
    this.pointer.set((at.x - box.left) / box.width - 0.5, (at.y - box.top) / box.height - 0.5);
    this.host.style.cursor = this.hit({ clientX: at.x, clientY: at.y }) ? "pointer" : "";
    this.hover = null;
  }

  private resize() {
    this.dirty = true;
    const { clientWidth: width, clientHeight: height } = this.host;
    this.renderer.setSize(width, height);
    this.camera.aspect = width / height;
    this.camera.position.z = width < height ? 12 : 8;
    this.camera.updateProjectionMatrix();
    this.span = 2 * this.camera.position.z * Math.tan(THREE.MathUtils.degToRad(this.camera.fov / 2)) * this.camera.aspect;
    this.sheets.forEach((sheet, i) => {
      const moved = sheet.target === sheet.rest;
      sheet.rest = this.fan(i, this.sheets.length);
      if (moved) sheet.target = sheet.rest;
    });
  }
}
