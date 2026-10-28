// Small presentational primitives shared across the Studio panels.
import { Loader2 } from 'lucide-react';

export const Badge = ({ children, tone = 'default', title }) => (
  <span className={`ui-badge ui-badge-${tone}`} title={title}>{children}</span>
);

/** Matches EmptyState's layout, for the moment before there's anything to
 * show empty or full (issue #132) - a spinning icon instead of a static one. */
export const LoadingState = ({ title, children }) => (
  <div className="ui-empty" role="status" aria-live="polite">
    <Loader2 size={40} className="ui-empty-icon anim-spin" />
    <h3>{title}</h3>
    {children && <p>{children}</p>}
  </div>
);

export const IconButton = ({ icon: Icon, label, active, tone, size = 14, ...rest }) => (
  <button
    type="button"
    className={`ui-icon-btn${active ? ' active' : ''}${tone ? ` tone-${tone}` : ''}`}
    title={label}
    aria-label={label}
    aria-pressed={active === undefined ? undefined : !!active}
    {...rest}
  >
    <Icon size={size} />
  </button>
);

export const NumberField = ({ label, value, onChange, min, max, step = 1, suffix }) => (
  <label className="ui-field">
    <span className="ui-field-label">{label}{suffix ? ` (${suffix})` : ''}</span>
    <input
      type="number"
      value={value}
      min={min}
      max={max}
      step={step}
      onChange={(e) => {
        // Keep the field editable while it's mid-edit (empty or "-"), but never
        // emit NaN to the caller's state.
        const raw = e.target.value;
        if (raw === '') return onChange(min ?? 0);
        const parsed = parseFloat(raw);
        if (Number.isNaN(parsed)) return;
        let v = parsed;
        if (min !== undefined) v = Math.max(min, v);
        if (max !== undefined) v = Math.min(max, v);
        onChange(v);
      }}
    />
  </label>
);

export const Slider = ({ label, value, onChange, min = 0, max = 1, step = 0.01, format }) => (
  <label className="ui-slider">
    <span className="ui-slider-head">
      <span>{label}</span>
      <span className="ui-slider-value">{format ? format(value) : value}</span>
    </span>
    <input
      type="range"
      min={min}
      max={max}
      step={step}
      value={value}
      onChange={(e) => onChange(parseFloat(e.target.value))}
    />
  </label>
);

export const EmptyState = ({ icon: Icon, title, children }) => (
  <div className="ui-empty">
    {Icon && <Icon size={40} className="ui-empty-icon" />}
    <h3>{title}</h3>
    {children && <p>{children}</p>}
  </div>
);
