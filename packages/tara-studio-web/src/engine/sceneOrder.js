// The one place that turns "move scene A before/after scene B" (or to the start /
// end) into the `to` index of sceneCommands.reorder(from, to). Shared by the typed
// grammar (intentParser.js) and the local planner (planCompile.js) so the two can
// never disagree about what "after" means.
//
// reorder(from, to) removes the scene, then inserts it at `to` in the shorter list.
// `from` and `anchor` are 0-based positions in the list the command will run against;
// `count` is that list's length (only needed for 'last').

/** @param {'before'|'after'|'first'|'last'} relation  @returns {number|null} null for an unknown relation */
export const moveTargetIndex = (from, anchor, relation, count) => {
  switch (relation) {
    case 'first': return 0;
    case 'last': return count - 1;
    case 'after': return from < anchor ? anchor : anchor + 1;
    case 'before': return from < anchor ? anchor - 1 : anchor;
    default: return null;
  }
};
