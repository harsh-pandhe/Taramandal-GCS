// "Latest run wins" gate for asynchronous work whose result belongs to one
// specific input.
//
// A show's backend validation runs in the background after the build returns
// and takes seconds at fleet scale (~15-20 s at 1000 drones). Meanwhile the
// designer can build again, or load another project. Without a gate, the
// *older* validation resolves later and overwrites the newer show's validation
// state - a stale PASSED shown against a show that was never validated, and the
// "Validating..." indicator cleared while the real validation is still running.
//
// begin() starts a run and returns its id, which also invalidates every earlier
// run. invalidate() does the same without starting one (the show was replaced
// from outside, e.g. a project load). A run may only apply its result, or clear
// shared in-flight state, while isCurrent(id) is true.
export const createLatestRun = () => {
  let latest = 0;
  return {
    begin: () => (latest += 1),
    invalidate: () => { latest += 1; },
    isCurrent: (id) => id === latest,
  };
};
