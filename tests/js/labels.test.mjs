/**
 * Unit tests for the pure part of `site/assets/js/labels.js`.
 *
 * The layer itself needs a DOM and a WebGL camera, but the decision that
 * governs the frame loop — "has the view moved since the labels were last
 * drawn?" — is pure, and it is the one place where a wrong answer would
 * silently strand every label away from its node.
 *
 * Importing the module is DOM-free: nothing touches `document` until
 * `createLabelLayer` is actually called.
 */

import test from "node:test";
import assert from "node:assert/strict";

import { samePose } from "../../site/assets/js/labels.js";

const pose = (x, y, z, look = { x: 0, y: 0, z: 0 }) => ({
  x,
  y,
  z,
  lookAt: { ...look },
});

test("samePose is true for an identical, separately-allocated pose", () => {
  // cameraPosition() hands back a fresh object every call, so identity is
  // never the thing being compared.
  assert.equal(samePose(pose(1, 2, 3), pose(1, 2, 3)), true);
});

test("samePose notices the camera moving", () => {
  assert.equal(samePose(pose(1, 2, 3), pose(1, 2, 3.5)), false);
  assert.equal(samePose(pose(0, 0, 0), pose(0, 0, 0.0001)), false);
});

test("samePose notices the camera turning in place", () => {
  // Same position, different look-at: an orbit drag that ends where it
  // started would otherwise freeze every label mid-rotation.
  const from = pose(10, 0, 0, { x: 0, y: 0, z: 0 });
  const turned = pose(10, 0, 0, { x: 0, y: 5, z: 0 });
  assert.equal(samePose(from, turned), false);
});

test("samePose treats a missing pose as moved", () => {
  // No previous frame means there is nothing to trust: draw.
  assert.equal(samePose(null, pose(1, 2, 3)), false);
  assert.equal(samePose(pose(1, 2, 3), null), false);
  assert.equal(samePose(undefined, undefined), false);
});

test("samePose tolerates a pose with no look-at", () => {
  const bare = { x: 1, y: 2, z: 3 };
  assert.equal(samePose(bare, { x: 1, y: 2, z: 3 }), true);
  assert.equal(samePose(bare, { x: 1, y: 2, z: 4 }), false);
});
