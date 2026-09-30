import { useState } from 'react';
import ConfirmButton from '../../ConfirmButton';

/**
 * Fleet-wide emergency commands, each behind a one-click confirmation (#98).
 * Kept as their own component so any future emergency action (E-STOP) lives
 * here too, apart from the ordinary launch control.
 */
export default function EmergencyActions({ triggerAction }) {
  // Which emergency action (if any) is waiting for its confirming click
  // (issue #98). One at a time, so a confirm can't land on the wrong action.
  const [armedEmergency, setArmedEmergency] = useState(null);

  return (
    <>
      <ConfirmButton
        className="btn-land"
        label="EMER LAND"
        confirmLabel="CONFIRM LAND?"
        armed={armedEmergency === 'land'}
        onArm={() => setArmedEmergency('land')}
        onDisarm={() => setArmedEmergency(null)}
        onConfirm={() => triggerAction('/api/land')}
      />

      <ConfirmButton
        className="btn-rtl"
        label="EMERGENCY RTL"
        confirmLabel="CONFIRM RTL?"
        armed={armedEmergency === 'rtl'}
        onArm={() => setArmedEmergency('rtl')}
        onDisarm={() => setArmedEmergency(null)}
        onConfirm={() => triggerAction('/api/rtl')}
      />

      {/* Motor kill: same one-click confirmation as RTL/LAND (#98), and
          arming it disarms them, so a confirm can't land on the wrong action. */}
      <ConfirmButton
        className="btn-rtl"
        label="E-STOP (KILL)"
        confirmLabel="CONFIRM KILL?"
        armed={armedEmergency === 'estop'}
        onArm={() => setArmedEmergency('estop')}
        onDisarm={() => setArmedEmergency(null)}
        onConfirm={() => triggerAction('/api/estop')}
      />
    </>
  );
}
