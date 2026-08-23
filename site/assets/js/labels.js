/**
 * DOM overlay labels for the 3D scene.
 *
 * Text drawn inside WebGL is either blurry or expensive, and it can't be
 * clicked with a keyboard. So the labels live in the DOM: one
 * `<button class="node-label">` per labelled node, moved every animation
 * frame to wherever its node currently projects on screen.
 *
 * Only a budget of nodes gets a label — every source, the modalities, the
 * biggest conditions and institutions, plus whatever the reader is
 * pointing at and its immediate neighbours. A dataset only ever gets one
 * while it is in that focus.
 */

/** Label budgets per node type for the resting scene. */
const ALWAYS = {
  source: Infinity,
  modality: Infinity,
  condition: 15,
  institution: 6,
};

/** Modalities are the first thing to thin out on a phone. */
const MOBILE_MODALITIES = 10;

/** How many neighbours of the hovered/selected node get a label. */
const EGO_MAX = 25;

/** Dataset names are long; the CSS ellipsis is a backstop, not a plan. */
const NAME_MAX = 28;

/** Nudge the text clear of the node it belongs to. */
const OFFSET_X = 9;
const OFFSET_Y = -9;

/** Beyond this multiple of the camera-to-centre distance, labels go away. */
const FAR_FACTOR = 2;

/** How far off the stage a label may sit before it is dropped. */
const MARGIN = 24;

function cmpId(a, b) {
  return a < b ? -1 : a > b ? 1 : 0;
}

/**
 * Do two camera poses describe the same view?
 *
 * `cameraPosition()` reports where the camera is and the point it looks
 * at, which between them pin down both translation and rotation. Exact
 * comparison is what we want: these are the very floats the last frame
 * was drawn from, so "unchanged" means bit-for-bit unchanged.
 *
 * Exported for testing; the frame loop is the only caller.
 */
export function samePose(a, b) {
  if (!a || !b) return false;
  const from = a.lookAt ?? {};
  const to = b.lookAt ?? {};
  return (
    a.x === b.x &&
    a.y === b.y &&
    a.z === b.z &&
    from.x === to.x &&
    from.y === to.y &&
    from.z === to.z
  );
}

function truncate(text, max) {
  const value = String(text ?? "");
  return value.length > max ? `${value.slice(0, max - 1)}…` : value;
}

/**
 * Create the overlay layer inside `host` (the graph stage).
 *
 * Options:
 * - `graph` — the ForceGraph3D instance (`graph2ScreenCoords`,
 *   `cameraPosition`).
 * - `index` — the graph index, for node lookups, degrees and neighbours.
 * - `getVisible()` — the ids currently in the scene, as a Set.
 * - `getFocus()` — `{hovered, selected}` ids, or nulls.
 * - `isDimmed(node)` — the node is filtered out, so its label reads quiet.
 * - `isSettled()` — the force engine has stopped, so nothing moves unless
 *   the camera does. Lets the frame loop skip its work while the reader
 *   just sits and looks.
 * - `onSelect(node)` — a label was clicked.
 * - `mobile` — trims the budgets.
 *
 * Returns `{refresh, start, stop, destroy}`. `refresh()` re-picks which
 * nodes deserve a label (cheap, but not per-frame work); the rAF loop only
 * moves the labels that already exist.
 */
export function createLabelLayer(
  host,
  {
    graph,
    index,
    getVisible,
    getFocus,
    isDimmed,
    isSettled,
    onSelect,
    mobile = false,
  } = {},
) {
  const layer = document.createElement("div");
  layer.className = "node-labels";
  layer.setAttribute("aria-label", "Graph labels");
  host.appendChild(layer);

  /** id -> button, so a label survives across refreshes. */
  const pool = new Map();
  let frameId = null;
  let running = false;

  /** The camera pose the labels were last positioned for. */
  let lastPose = null;

  layer.addEventListener("click", (event) => {
    const button = event.target.closest("button[data-id]");
    if (!button) return;
    const node = index.nodes.get(button.dataset.id);
    if (node) onSelect?.(node);
  });

  /** The ids that should carry a label right now. */
  function pick() {
    const visible = getVisible?.() ?? new Set();
    const focus = getFocus?.() ?? {};

    const byType = new Map();
    for (const id of visible) {
      const node = index.nodes.get(id);
      if (!node || node.type === "dataset") continue;
      if (!byType.has(node.type)) byType.set(node.type, []);
      byType.get(node.type).push(id);
    }

    const chosen = new Set();
    for (const [type, list] of byType) {
      let cap = ALWAYS[type] ?? 0;
      if (mobile && type === "modality") cap = MOBILE_MODALITIES;
      if (cap === 0) continue;

      list.sort((a, b) => index.degreeOf(b) - index.degreeOf(a) || cmpId(a, b));
      for (const id of Number.isFinite(cap) ? list.slice(0, cap) : list) {
        chosen.add(id);
      }
    }

    // The reader's current attention wins a few extra labels: the node
    // itself and the strongest of its neighbours, including datasets.
    for (const anchor of [focus.selected, focus.hovered]) {
      if (!anchor || !visible.has(anchor)) continue;
      chosen.add(anchor);
      let budget = EGO_MAX;
      for (const id of index.neighbours(anchor)) {
        if (budget <= 0) break;
        if (!visible.has(id)) continue;
        chosen.add(id);
        budget -= 1;
      }
    }
    return chosen;
  }

  function labelFor(node) {
    const text = node.label ?? node.id;
    return node.type === "dataset" ? truncate(text, NAME_MAX) : String(text);
  }

  /** Reconcile the button pool with the current pick. */
  function refresh() {
    const wanted = pick();
    const focus = getFocus?.() ?? {};
    const hasFocus = Boolean(focus.selected || focus.hovered);

    for (const [id, button] of pool) {
      if (wanted.has(id)) continue;
      button.remove();
      pool.delete(id);
    }

    for (const id of wanted) {
      const node = index.nodes.get(id);
      if (!node) continue;

      let button = pool.get(id);
      if (!button) {
        button = document.createElement("button");
        button.type = "button";
        button.className = "node-label";
        button.dataset.id = id;
        button.style.visibility = "hidden";
        layer.appendChild(button);
        pool.set(id, button);
      }

      const text = labelFor(node);
      if (button.textContent !== text) button.textContent = text;

      // De-emphasise everything that isn't the point right now: the
      // secondary hub tiers at rest, everything outside the ego in focus.
      const inFocus =
        id === focus.selected ||
        id === focus.hovered ||
        (hasFocus && index.adj.get(focus.selected ?? focus.hovered)?.has(id));
      const quiet = isDimmed?.(node)
        ? true // filtered out by the legend
        : hasFocus
          ? !inFocus
          : node.type === "condition" || node.type === "institution";

      if (quiet) button.dataset.quiet = "true";
      else delete button.dataset.quiet;
    }

    position();
  }

  /** Move every label to its node's current screen position. */
  function position(pose) {
    if (pool.size === 0) return;

    // Read the layout once, before any writes: interleaving the two would
    // force a synchronous reflow on every label, every frame. Zero means
    // the box has never been laid out (a backgrounded tab skips layout
    // entirely), in which case there is no stage to clip against yet.
    const width = layer.clientWidth;
    const height = layer.clientHeight;
    const clip = width > 0 && height > 0;

    const camera = pose ?? graph.cameraPosition();
    lastPose = camera;
    const target = camera?.lookAt ?? { x: 0, y: 0, z: 0 };
    let fx = target.x - camera.x;
    let fy = target.y - camera.y;
    let fz = target.z - camera.z;
    const centreDist = Math.hypot(fx, fy, fz) || 1;
    fx /= centreDist;
    fy /= centreDist;
    fz /= centreDist;
    const farDist = centreDist * FAR_FACTOR;

    for (const [id, button] of pool) {
      const node = index.nodes.get(id);
      if (!node || !Number.isFinite(node.x)) {
        hide(button);
        continue;
      }

      const dx = node.x - camera.x;
      const dy = node.y - camera.y;
      const dz = node.z - camera.z;

      // `graph2ScreenCoords` projects points behind the camera to a
      // mirrored position on screen, so depth has to be checked first.
      const depth = dx * fx + dy * fy + dz * fz;
      if (depth <= 0 || Math.hypot(dx, dy, dz) > farDist) {
        hide(button);
        continue;
      }

      const point = graph.graph2ScreenCoords(node.x, node.y, node.z);
      if (!point || !Number.isFinite(point.x)) {
        hide(button);
        continue;
      }
      // Off-stage labels are clipped anyway; hiding them keeps them out of
      // the tab order and off the frame's work list.
      if (
        clip &&
        (point.x < -MARGIN ||
          point.y < -MARGIN ||
          point.x > width + MARGIN ||
          point.y > height + MARGIN)
      ) {
        hide(button);
        continue;
      }

      button.style.transform =
        `translate3d(${Math.round(point.x) + OFFSET_X}px, ${Math.round(point.y) + OFFSET_Y}px, 0)`;
      if (button.style.visibility !== "visible") button.style.visibility = "visible";
    }
  }

  function hide(button) {
    if (button.style.visibility !== "hidden") button.style.visibility = "hidden";
  }

  function frame() {
    if (!running) return;
    // While the layout runs, every node is moving and every frame counts.
    // Once it has settled, only the camera can change where a label
    // belongs — hover and selection changes arrive through refresh().
    if (!isSettled?.()) {
      position();
    } else {
      const pose = graph.cameraPosition();
      if (!samePose(lastPose, pose)) position(pose);
    }
    frameId = requestAnimationFrame(frame);
  }

  function start() {
    if (running) return;
    running = true;
    frameId = requestAnimationFrame(frame);
  }

  function stop() {
    running = false;
    if (frameId !== null) cancelAnimationFrame(frameId);
    frameId = null;
  }

  function destroy() {
    stop();
    lastPose = null;
    pool.clear();
    layer.remove();
  }

  return { refresh, position, start, stop, destroy };
}
