import { useEffect, useRef } from 'react';

/**
 * Scrolls itself into view (nearest edge, so nothing moves when it is already
 * visible) whenever `watch` changes. Put it at the END of a block that appears
 * below the fold of a scrolling panel, so the whole block, not just its top,
 * ends up on screen.
 *
 * A separate component, rendered only once there is something to show, so the
 * panels that use it stay free of hooks and keep their no-DOM unit tests.
 */
export default function ScrollIntoView({ watch }) {
  const ref = useRef(null);
  useEffect(() => {
    ref.current?.scrollIntoView?.({ block: 'nearest', behavior: 'smooth' });
  }, [watch]);
  return <span ref={ref} aria-hidden="true" data-testid="scroll-anchor" />;
}
