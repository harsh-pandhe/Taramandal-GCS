// A refused show: well-formed input the safety rules won't allow (spacing,
// altitude, site radius...), as opposed to a bug in the code that produced it.
//
// Call sites that run during React's render phase (a useMemo computing a
// preview or a predicted duration from not-yet-built state) must not let an
// uncaught throw reach render — React unmounts the whole tree on one. But
// catching every exception there would also swallow genuine bugs as a silent
// blank preview. Catching this type specifically keeps both properties: a
// refusal degrades to an inline message, anything else still crashes loudly
// (issue #141, #153).
export class ShowRefusalError extends Error {
  constructor(message) {
    super(message);
    this.name = 'ShowRefusalError';
  }
}
