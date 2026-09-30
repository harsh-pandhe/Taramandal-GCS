/** The dashboard-wide error line: the last failed command or request. */
export default function ErrorBanner({ message }) {
  if (!message) return null;
  return (
    <div style={{
      background: 'var(--danger-tint)',
      border: '1px solid var(--danger)',
      padding: '1rem',
      borderRadius: 'var(--r)',
      color: 'var(--danger)',
      fontWeight: 600,
      fontSize: '0.85rem',
      fontFamily: 'var(--font-mono)'
    }}>
      {message}
    </div>
  );
}
