// Ranges a scene edit may take (issue #89). These mirror the min/max the Studio
// UI already enforces on its number fields (SceneCard.jsx and
// ShapeLibraryModal.jsx); sceneLimits.test.js reads those files and fails if
// the numbers drift. They bound scene *edits* only - they are not flight or
// safety limits and nothing here can change one.

export const SCENE_LIMITS = Object.freeze({
  holdDurationSeconds: Object.freeze({ min: 0, max: 120 }),
  transitionDurationSeconds: Object.freeze({ min: 0, max: 60 }),
  transitLayerGapMeters: Object.freeze({ min: 0.5, max: 6 }),
  scale: Object.freeze({ min: 0.25, max: 3 }),
});

export const MAX_SCENE_NAME_LENGTH = 60;
