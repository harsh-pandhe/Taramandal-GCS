import { describe, expect, it } from 'vitest';
import { renderToStaticMarkup } from 'react-dom/server';
import RasterImportModal from './RasterImportModal.jsx';

describe('RasterImportModal', () => {
  it('renders initial dropzone state without crashing', () => {
    const html = renderToStaticMarkup(
      <RasterImportModal numDrones={35} onInsertScene={() => {}} onClose={() => {}} />
    );
    expect(html).toContain('Image to Formation Converter');
    expect(html).toContain('Click or drop image');
    expect(html).toContain('Target Fleet Drones (N)');
    expect(html).toContain('Formation Scale');
    expect(html).toContain('Flight Altitude');
    expect(html).toContain('Auto Otsu Threshold');
    expect(html).toContain('Insert as Scene');
  });

  it('renders with custom fleet size in input', () => {
    const html = renderToStaticMarkup(
      <RasterImportModal numDrones={75} onInsertScene={() => {}} onClose={() => {}} />
    );
    expect(html).toContain('value="75"');
  });
});
