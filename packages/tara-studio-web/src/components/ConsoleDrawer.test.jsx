import { describe, expect, it } from 'vitest';
import { findAll } from '../testing/elementTree.js';
import ConsoleDrawer from './ConsoleDrawer.jsx';
import ProposalPanel from './panels/ProposalPanel.jsx';
import AuditLogPanel from './panels/AuditLogPanel.jsx';
import AIPanel from './panels/AIPanel.jsx';

// The drawer is a plain function component, so what it returns for a tab can be read directly.
const drawer = (tab, over = {}) => ConsoleDrawer({
  open: true, onClose() {}, tab, onTab() {}, list: {}, history: {}, loading: false, onBuild() {}, onOpenLibrary() {},
  onOpenRasterImport() {}, pacing: 'standard', onPacing() {}, light: {}, choreoData: null, currentTime: 0, lightingOpts: {},
  validation: null, validating: false, venue: {}, transmit: {}, audio: {}, ai: {}, intent: { text: '' }, segments: [],
  stale: false, onScrub() {}, onSelectScene() {}, ...over,
});
const contentTypes = (tree) => {
  const [content] = findAll(tree, (n) => n.props?.className === 'drawer-content');
  return [content.props.children].flat().filter(Boolean).flatMap((c) => (c.type === Symbol.for('react.fragment') || c.type?.toString?.() === 'Symbol(react.fragment)' ? [c.props.children].flat() : [c])).filter(Boolean).map((c) => c.type);
};

describe('the AI tab', () => {
  it('shows the AI panel, the scene edit panel, and then the decision log, in that order', () => {
    expect(contentTypes(drawer('ai'))).toEqual([AIPanel, ProposalPanel, AuditLogPanel]);
  });

  it('gives the decision log the same intent the scene edit panel has', () => {
    const intent = { text: '', auditVersion: 3 };
    const tree = drawer('ai', { intent });
    const [proposal] = findAll(tree, (n) => n.type === ProposalPanel);
    const [log] = findAll(tree, (n) => n.type === AuditLogPanel);
    expect(log.props.intent).toBe(intent);
    expect(proposal.props.intent).toBe(intent);
  });

  it('is not mounted without an intent (the panel renders only where the proposal hook exists)', () => {
    expect(contentTypes(drawer('ai', { intent: null }))).toEqual([AIPanel]);
  });
});

describe('the other tabs', () => {
  it.each(['sequence', 'light', 'audio', 'safety', 'transmit'])('%s does not mount the decision log or the scene edit panel', (tab) => {
    const tree = drawer(tab);
    expect(findAll(tree, (n) => n.type === AuditLogPanel)).toHaveLength(0);
    expect(findAll(tree, (n) => n.type === ProposalPanel)).toHaveLength(0);
  });

  it('there is still one tab button per tab, with the same names', () => {
    const tabs = findAll(drawer('ai'), (n) => n.props?.className?.startsWith?.('drawer-tab-btn'));
    expect(tabs.map((t) => t.props.children)).toEqual(['SEQUENCE', 'LIGHT', 'AUDIO', 'AI', 'SAFETY', 'TRANSMIT']);
  });
});
