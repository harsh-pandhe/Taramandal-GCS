import React from 'react';

/**
 * Catches render/lifecycle errors anywhere in the tree and shows a recoverable
 * error panel instead of unmounting to a blank white screen.
 */
export default class ErrorBoundary extends React.Component {
  constructor(props) {
    super(props);
    this.state = { error: null };
  }

  static getDerivedStateFromError(error) {
    return { error };
  }

  componentDidCatch(error, info) {
    // Surface to the console for debugging; the UI stays usable.
    console.error('Studio render error:', error, info?.componentStack);
  }

  handleReset = () => {
    this.setState({ error: null });
  };

  render() {
    if (this.state.error) {
      return (
        <div className="error-boundary-wrapper">
          <div className="error-boundary-card">
            <div className="error-boundary-badge">RUNTIME FAULT</div>
            <h2>Studio hit a rendering error</h2>
            <p className="error-boundary-msg">{String(this.state.error?.message || this.state.error)}</p>
            <p className="error-boundary-hint">
              Your projects are safe. Recover the view, or reload if it persists.
            </p>
            <div className="error-boundary-actions">
              <button className="btn-primary" onClick={this.handleReset}>Recover View</button>
              <button className="btn-secondary" onClick={() => window.location.reload()}>Reload Studio</button>
            </div>
          </div>
        </div>
      );
    }
    return this.props.children;
  }
}
