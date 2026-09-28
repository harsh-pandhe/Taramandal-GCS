// Test harness: mounts the real PreflightModal and usePreflight hook against
// the same-origin /api/* paths, which the spec answers with page.route(). The
// gate verdicts are the GCS's job and are tested in tara-gcs; what this
// harness isolates is whether the dashboard honours them — a red report must
// leave the launch button unusable, whatever the rest of the UI is doing.

import React, { useState } from 'react';
import { createRoot } from 'react-dom/client';
import { PreflightModal } from '../../src/PreflightChecklist';
import { usePreflight } from '../../src/preflight';

// Fast polling so a report change reaches the modal well inside expect()'s timeout.
const POLL_MS = 150;

function Harness() {
  const { report, error } = usePreflight('', POLL_MS);
  const [launched, setLaunched] = useState(null);
  return (
    <>
      <PreflightModal apiBase="" report={report} error={error} onClose={() => {}} onLaunched={setLaunched} />
      {launched && <div data-testid="launched">{launched.drone_ids.join(',')}</div>}
    </>
  );
}

createRoot(document.getElementById('root')).render(<Harness />);
