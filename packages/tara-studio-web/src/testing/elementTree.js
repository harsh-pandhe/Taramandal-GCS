// Helpers for tests that call a function component directly (with React's
// state hooks stubbed) and drive the elements it returns. The package has no
// DOM testing stack (see ShapeLibraryModal.test.jsx), and a click handler is
// only reachable this way.

/** Every element in a returned tree for which `match(element)` is true. */
export const findAll = (node, match, found = []) => {
  if (Array.isArray(node)) node.forEach((n) => findAll(n, match, found));
  else if (node && typeof node === 'object' && node.props) {
    if (match(node)) found.push(node);
    findAll(node.props.children, match, found);
  }
  return found;
};

export const textOf = (node) => {
  if (Array.isArray(node)) return node.map(textOf).join('');
  if (node && typeof node === 'object') return textOf(node.props?.children);
  return node == null || typeof node === 'boolean' ? '' : String(node);
};

/** The `<button>` whose text contains `label`; throws if there isn't exactly one. */
export const button = (tree, label) => {
  const hits = findAll(tree, (n) => n.type === 'button' && textOf(n).includes(label));
  if (hits.length !== 1) throw new Error(`expected one button "${label}", found ${hits.length}`);
  return hits[0];
};

/** A minimal useState: state lives in `slots`; reset `i` to 0 before each render. */
export const makeStateStub = () => {
  const stub = { slots: [], i: 0 };
  stub.useState = (init) => {
    const i = stub.i++;
    if (!(i in stub.slots)) stub.slots[i] = init;
    return [stub.slots[i], (v) => { stub.slots[i] = typeof v === 'function' ? v(stub.slots[i]) : v; }];
  };
  return stub;
};
