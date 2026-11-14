import { describe, expect, it, vi } from 'vitest';
import { button, findAll, textOf } from '../../testing/elementTree.js';
import NeedsAttention from './NeedsAttention.jsx';
import SceneCard from './SceneCard.jsx';

// No DOM here: the component has no hooks, so it is called as a function and
// the elements it returns are inspected and driven directly.
const SEGS = [
  { kind: 'takeoff', label: 'Takeoff', sceneId: null, start: 0, end: 5 },
  { kind: 'morph', label: 'Climb-out', sceneId: 'a', start: 5, end: 9 },
  { kind: 'hold', label: 'Five', sceneId: 'a', start: 9, end: 15 },
  { kind: 'morph', label: 'Morph → Four', sceneId: 'b', start: 15, end: 19 },
  { kind: 'hold', label: 'Four', sceneId: 'b', start: 19, end: 25 },
  { kind: 'morph', label: 'Return', sceneId: 'b', start: 25, end: 29 },
  { kind: 'land', label: 'Landing', sceneId: null, start: 29, end: 34 },
];
const SCENES = [{ id: 'a', name: 'Five' }, { id: 'b', name: 'Four' }];
const FAILED = {
  status: 'FAILED',
  violations: {
    proximity: [
      { time: 10.1, drones: [2, 3], distance: 1.2 }, { time: 10.2, drones: [2, 3], distance: 1.25 },
      { time: 10.3, drones: [2, 3], distance: 1.3 }, { time: 10.4, drones: [2, 3], distance: 1.35 },
      { time: 20.4, drones: [0, 1], distance: 1.4 },
    ],
    velocity: [{ time: 21, drone: 4, velocity: 4.8 }, { time: 2, drone: 1, velocity: 9 }],
  },
};
const render = (over = {}) => NeedsAttention({ validation: FAILED, segments: SEGS, scenes: SCENES, stale: false, ...over });

describe('NeedsAttention', () => {
  it('renders nothing unless the check FAILED and there is something to show', () => {
    expect(render({ validation: { ...FAILED, status: 'PASSED' } })).toBeNull();
    expect(render({ validation: { status: 'UNVERIFIED', summary: 'offline' } })).toBeNull();
    expect(render({ validation: null })).toBeNull();
    expect(render({ validation: undefined })).toBeNull();
    expect(render({ segments: [] })).toBeNull();
    expect(render({ validation: { status: 'FAILED', violations: {} } })).toBeNull();
    expect(NeedsAttention({})).toBeNull();
  });

  it('shows a block per failing scene, with counts and at most 3 explanations', () => {
    const tree = render();
    const text = textOf(tree);
    expect(text).toContain('Needs attention');
    expect(text).toContain('01 · Five');
    expect(text).toContain('4 proximity');
    expect(text).toContain('02 · Four');
    expect(text).toContain('1 proximity, 1 velocity');
    expect(text).toContain('Drones 2 and 3 came 1.2 m apart at 10.1 s');
    expect(text).not.toContain('came 1.35 m apart'); // the 4th of scene Five is folded away
    expect(text).toContain('+1 more');
    expect(text).toContain('up to its reporting cap');
    expect(findAll(tree, (n) => n.type === 'button' && textOf(n) === 'Go to scene')).toHaveLength(2);
  });

  it('lists takeoff/landing items separately, with a jump-to-time button only', () => {
    const tree = render();
    expect(textOf(tree)).toContain('Not tied to a scene');
    expect(textOf(tree)).toContain('Drone 1 reached 9.0 m/s at 2.0 s');
    const jump = vi.fn();
    button(render({ onScrub: jump }), 'Jump to time').props.onClick();
    expect(jump).toHaveBeenCalledWith(2);
  });

  it('Go to scene selects the scene and jumps to its first violation time', () => {
    const onSelectScene = vi.fn();
    const onScrub = vi.fn();
    const tree = render({ onSelectScene, onScrub });
    const [first, second] = findAll(tree, (n) => n.type === 'button' && textOf(n) === 'Go to scene');
    first.props.onClick();
    expect(onSelectScene).toHaveBeenLastCalledWith('a');
    expect(onScrub).toHaveBeenLastCalledWith(10.1);
    second.props.onClick();
    expect(onSelectScene).toHaveBeenLastCalledWith('b');
    expect(onScrub).toHaveBeenLastCalledWith(20.4);
  });

  it('does not throw when clicked without handlers', () => {
    const [first] = findAll(render(), (n) => n.type === 'button' && textOf(n) === 'Go to scene');
    expect(() => first.props.onClick()).not.toThrow();
  });

  it('when stale shows only the note, with no links', () => {
    const tree = render({ stale: true });
    expect(textOf(tree)).toContain('The scenes changed since the last safety check. Build again to refresh this list.');
    expect(findAll(tree, (n) => n.type === 'button')).toHaveLength(0);
    expect(textOf(tree)).not.toContain('Five');
    expect(NeedsAttention({ validation: { ...FAILED, status: 'PASSED' }, stale: true, segments: SEGS, scenes: SCENES })).toBeNull();
  });
});

describe('SceneCard attention marker', () => {
  const card = (over = {}) => SceneCard({
    scene: { id: 'a', name: 'Five', shapeType: 'text', shapePayload: '5', scale: 1, holdDurationSeconds: 6, transitionDurationSeconds: 4 },
    index: 0, total: 2, onPatch() {}, onDuplicate() {}, onDelete() {}, onMove() {}, onOpenLibrary() {},
    onDragStart() {}, onDragOver() {}, onDrop() {}, onDragEnd() {}, ...over,
  });
  it('adds a class only when asked, and is off by default', () => {
    expect(card().props.className).toBe('scene-card');
    expect(card({ attention: true }).props.className).toBe('scene-card attention');
  });
});
