/**
 * Deterministic, safe SVG-to-formation parser and resampler (issue #129).
 *
 * Converts safe SVG path outlines into exactly N drone positions meeting the
 * 2.0 m minimum design spacing requirement, without executing scripts or
 * embedding untrusted markup into the DOM.
 */

const MAX_SVG_BYTES = 500_000;
export const MIN_DESIGN_SPACING_M = 2.0;
export const SITE_RADIUS_M = 30.0;
export const MAX_ALTITUDE_M = 120.0;

const FORBIDDEN_PATTERNS = [
  /<script/i,
  /<foreignobject/i,
  /<use/i,
  /<iframe/i,
  /<embed/i,
  /<image/i,
  /<!DOCTYPE/i,
  /xlink:href/i,
  /href\s*=\s*['"](http|data|javascript)/i,
  /\bon\w+\s*=/i,
  /<!ENTITY/i,
  /SYSTEM/i,
];

/**
 * Validates and sanitizes raw SVG markup.
 * Throws explicit errors on malformed, oversized, or unsafe input.
 */
export function sanitizeSvg(svgText) {
  if (typeof svgText !== 'string' || !svgText.trim()) {
    throw new Error('SVG content must not be empty.');
  }
  if (svgText.length > MAX_SVG_BYTES) {
    throw new Error(`SVG exceeds maximum size limit of ${MAX_SVG_BYTES} bytes.`);
  }
  for (const pattern of FORBIDDEN_PATTERNS) {
    if (pattern.test(svgText)) {
      throw new Error(`Unsafe SVG content rejected: matches forbidden pattern ${pattern}.`);
    }
  }
  return svgText;
}

/**
 * Validates viewBox attribute if present.
 */
export function validateViewBox(svgText) {
  const vbMatch = /<svg[^>]*\bviewBox=['"]([^'"]+)['"]/i.exec(svgText);
  if (!vbMatch) return null;
  const parts = vbMatch[1].trim().split(/[\s,]+/).map(Number);
  if (parts.length !== 4 || parts.some((p) => isNaN(p))) {
    throw new Error(`Malformed SVG viewBox: '${vbMatch[1]}'. Expected 4 numeric values.`);
  }
  const [minX, minY, width, height] = parts;
  if (width <= 0 || height <= 0) {
    throw new Error(`Invalid SVG viewBox: width (${width}) and height (${height}) must be positive.`);
  }
  return { minX, minY, width, height };
}

/**
 * Parses simple 2D transforms (translate, scale).
 * Rejects unsupported or malformed transforms explicitly.
 */
export function parseTransform(transformStr) {
  if (!transformStr || !transformStr.trim()) return (pt) => pt;
  const str = transformStr.trim();

  // Explicitly reject unsupported transform types
  const unsupported = /(?:matrix|rotate|skewX|skewY)\s*\(/i;
  if (unsupported.test(str)) {
    throw new Error(`Unsupported SVG transform: '${transformStr}'. Only translate and scale transforms are supported.`);
  }

  const transformRegex = /(translate|scale)\s*\(\s*([+-]?(?:\d*\.\d+|\d+)(?:[eE][+-]?\d+)?)(?:[\s,]+([+-]?(?:\d*\.\d+|\d+)(?:[eE][+-]?\d+)?))?\s*\)/gi;
  const funcs = [];
  let lastIndex = 0;
  let match;

  while ((match = transformRegex.exec(str)) !== null) {
    const prefix = str.slice(lastIndex, match.index).trim();
    if (prefix && prefix !== ',') {
      throw new Error(`Malformed SVG transform: unrecognized syntax near '${prefix}'.`);
    }
    lastIndex = transformRegex.lastIndex;

    const op = match[1].toLowerCase();
    const arg1 = Number(match[2]);
    const arg2 = match[3] !== undefined ? Number(match[3]) : (op === 'scale' ? arg1 : 0);

    if (isNaN(arg1) || isNaN(arg2)) {
      throw new Error(`Malformed SVG transform parameters in '${match[0]}'.`);
    }

    if (op === 'translate') {
      funcs.push((pt) => ({ x: pt.x + arg1, y: pt.y + arg2 }));
    } else if (op === 'scale') {
      funcs.push((pt) => ({ x: pt.x * arg1, y: pt.y * arg2 }));
    }
  }

  const trailing = str.slice(lastIndex).trim();
  if (trailing) {
    throw new Error(`Malformed or unsupported SVG transform: '${trailing}'.`);
  }

  return (pt) => funcs.reduceRight((acc, fn) => fn(acc), pt);
}

const SUPPORTED_COMMANDS = new Set(['M', 'L', 'H', 'V', 'C', 'Q', 'Z']);

/**
 * Tokenizes and parses SVG path commands (M, L, H, V, C, Q, Z) into polyline points.
 * Correctly parses negative numbers and scientific exponent notation (e.g. 1e-2, -4.5E+1).
 * Rejects unsupported commands (e.g. A, S, T) and malformed syntax explicitly.
 */
export function parsePathCommands(d) {
  if (typeof d !== 'string' || !d.trim()) {
    throw new Error('Malformed SVG path: path data string is empty.');
  }

  const contours = [];
  let curContour = [];
  let curX = 0, curY = 0;
  let startX = 0, startY = 0;

  // Tokenize into commands, numeric tokens (with signs, decimals, exponents), or unrecognized chars
  const tokenRegex = /([a-df-z])|([+-]?(?:\d*\.\d+|\d+)(?:[eE][+-]?\d+)?)|([^\s,]+)/gi;
  const tokens = [];
  let m;

  while ((m = tokenRegex.exec(d)) !== null) {
    if (m[1]) {
      tokens.push({ type: 'cmd', val: m[1] });
    } else if (m[2]) {
      const num = Number(m[2]);
      if (isNaN(num)) {
        throw new Error(`Malformed SVG path data: invalid number '${m[2]}'.`);
      }
      tokens.push({ type: 'num', val: num });
    } else {
      throw new Error(`Malformed SVG path data: unexpected token '${m[3]}'.`);
    }
  }

  let i = 0;
  let cmd = '';

  if (tokens.length === 0) {
    throw new Error('Malformed SVG path: no valid tokens found.');
  }

  if (tokens[0].type !== 'cmd' || (tokens[0].val.toUpperCase() !== 'M')) {
    throw new Error(`Malformed SVG path data: path must start with MoveTo (M or m), got '${tokens[0].val}'.`);
  }

  while (i < tokens.length) {
    const t = tokens[i];
    if (t.type === 'cmd') {
      cmd = t.val;
      i++;
    }

    const upper = cmd.toUpperCase();
    const isRel = cmd === cmd.toLowerCase();

    if (!SUPPORTED_COMMANDS.has(upper)) {
      throw new Error(`Unsupported SVG path command '${cmd}'. Supported commands are M, L, H, V, C, Q, Z.`);
    }

    const readNum = (context) => {
      if (i >= tokens.length || tokens[i].type !== 'num') {
        throw new Error(`Malformed SVG path data: missing coordinate for command '${cmd}' (${context}).`);
      }
      return tokens[i++].val;
    };

    if (upper === 'M') {
      if (curContour.length >= 2) {
        contours.push(curContour);
      }
      curContour = [];
      const x = readNum('X');
      const y = readNum('Y');
      curX = isRel ? curX + x : x;
      curY = isRel ? curY + y : y;
      startX = curX;
      startY = curY;
      curContour.push({ x: curX, y: curY });
      // Subsequent coordinate pairs are treated as implicit LineTo
      cmd = isRel ? 'l' : 'L';
    } else if (upper === 'L') {
      const x = readNum('X');
      const y = readNum('Y');
      curX = isRel ? curX + x : x;
      curY = isRel ? curY + y : y;
      curContour.push({ x: curX, y: curY });
    } else if (upper === 'H') {
      const x = readNum('X');
      curX = isRel ? curX + x : x;
      curContour.push({ x: curX, y: curY });
    } else if (upper === 'V') {
      const y = readNum('Y');
      curY = isRel ? curY + y : y;
      curContour.push({ x: curX, y: curY });
    } else if (upper === 'C') {
      const x1 = readNum('ctrl1 X');
      const y1 = readNum('ctrl1 Y');
      const x2 = readNum('ctrl2 X');
      const y2 = readNum('ctrl2 Y');
      const x = readNum('end X');
      const y = readNum('end Y');
      const p1x = isRel ? curX + x1 : x1, p1y = isRel ? curY + y1 : y1;
      const p2x = isRel ? curX + x2 : x2, p2y = isRel ? curY + y2 : y2;
      const endX = isRel ? curX + x : x, endY = isRel ? curY + y : y;

      // Sample cubic bezier curve into 8 steps
      for (let s = 1; s <= 8; s++) {
        const u = s / 8;
        const u1 = 1 - u;
        const bx = u1**3 * curX + 3 * u1**2 * u * p1x + 3 * u1 * u**2 * p2x + u**3 * endX;
        const by = u1**3 * curY + 3 * u1**2 * u * p1y + 3 * u1 * u**2 * p2y + u**3 * endY;
        curContour.push({ x: bx, y: by });
      }
      curX = endX;
      curY = endY;
    } else if (upper === 'Q') {
      const x1 = readNum('ctrl X');
      const y1 = readNum('ctrl Y');
      const x = readNum('end X');
      const y = readNum('end Y');
      const p1x = isRel ? curX + x1 : x1, p1y = isRel ? curY + y1 : y1;
      const endX = isRel ? curX + x : x, endY = isRel ? curY + y : y;

      for (let s = 1; s <= 6; s++) {
        const u = s / 6;
        const u1 = 1 - u;
        const bx = u1**2 * curX + 2 * u1 * u * p1x + u**2 * endX;
        const by = u1**2 * curY + 2 * u1 * u * p1y + u**2 * endY;
        curContour.push({ x: bx, y: by });
      }
      curX = endX;
      curY = endY;
    } else if (upper === 'Z') {
      curX = startX;
      curY = startY;
      curContour.push({ x: curX, y: curY });
      if (curContour.length >= 2) {
        contours.push(curContour);
      }
      curContour = [];
    }
  }

  if (curContour.length >= 2) {
    contours.push(curContour);
  }

  return contours;
}

/**
 * Extracts 2D contours from safe SVG elements.
 */
export function parseSvgToContours(svgText) {
  const clean = sanitizeSvg(svgText);
  validateViewBox(clean);
  const contours = [];

  // Match <path ... />
  const pathTagRegex = /<path\b([^>]*)\/?>/gi;
  let match;
  while ((match = pathTagRegex.exec(clean)) !== null) {
    const attrs = match[1];
    const dMatch = /\bd=['"]([^'"]+)['"]/i.exec(attrs);
    if (!dMatch) continue;
    const transformMatch = /\btransform=['"]([^'"]+)['"]/i.exec(attrs);
    const xform = parseTransform(transformMatch ? transformMatch[1] : null);

    const sub = parsePathCommands(dMatch[1]);
    for (const pts of sub) {
      if (pts.length >= 2) {
        contours.push(pts.map(xform));
      }
    }
  }

  // Match raw path string if no XML tag
  if (contours.length === 0 && (clean.trim().startsWith('M') || clean.trim().startsWith('m'))) {
    const sub = parsePathCommands(clean.trim());
    for (const pts of sub) {
      if (pts.length >= 2) contours.push(pts);
    }
  }

  // Match <polygon ... /> and <polyline ... />
  const polyTagRegex = /<(polygon|polyline)\b([^>]*)\/?>/gi;
  while ((match = polyTagRegex.exec(clean)) !== null) {
    const attrs = match[2];
    const pointsMatch = /\bpoints=['"]([^'"]+)['"]/i.exec(attrs);
    if (!pointsMatch) continue;
    const transformMatch = /\btransform=['"]([^'"]+)['"]/i.exec(attrs);
    const xform = parseTransform(transformMatch ? transformMatch[1] : null);

    const nums = pointsMatch[1].trim().split(/[\s,]+/).map(Number);
    if (nums.some((n) => isNaN(n)) || nums.length < 4 || nums.length % 2 !== 0) {
      throw new Error('Malformed SVG polygon/polyline points data.');
    }
    const pts = [];
    for (let k = 0; k < nums.length - 1; k += 2) {
      pts.push(xform({ x: nums[k], y: nums[k + 1] }));
    }
    if (pts.length >= 2) contours.push(pts);
  }

  if (contours.length === 0) {
    throw new Error('SVG contains no valid drawable paths or polygons.');
  }

  return contours;
}

/**
 * Resamples contours into exactly N points with uniform arc length.
 */
export function sampleSvgContours(contours, n, scaleMeters = 20.0) {
  if (n < 1) return [];

  let totalLength = 0;
  const contourLengths = contours.map((c) => {
    let len = 0;
    for (let j = 0; j < c.length - 1; j++) {
      len += Math.hypot(c[j + 1].x - c[j].x, c[j + 1].y - c[j].y);
    }
    totalLength += len;
    return len;
  });

  if (totalLength <= 1e-6) {
    throw new Error('Degenerate SVG geometry (zero path length).');
  }

  let minX = Infinity, maxX = -Infinity, minY = Infinity, maxY = -Infinity;
  for (const c of contours) {
    for (const p of c) {
      if (p.x < minX) minX = p.x;
      if (p.x > maxX) maxX = p.x;
      if (p.y < minY) minY = p.y;
      if (p.y > maxY) maxY = p.y;
    }
  }

  const spanX = Math.max(maxX - minX, 1e-4);
  const spanY = Math.max(maxY - minY, 1e-4);
  const extent = Math.max(spanX, spanY);
  const midX = (minX + maxX) / 2;
  const midY = (minY + maxY) / 2;

  const sampled = [];
  const step = totalLength / n;

  for (let idx = 0; idx < n; idx++) {
    const targetDist = (idx + 0.5) * step;
    let accumulated = 0;
    let pt = null;

    for (let cIdx = 0; cIdx < contours.length; cIdx++) {
      const c = contours[cIdx];
      const cLen = contourLengths[cIdx];

      if (accumulated + cLen >= targetDist || cIdx === contours.length - 1) {
        let segDist = targetDist - accumulated;
        for (let j = 0; j < c.length - 1; j++) {
          const d = Math.hypot(c[j + 1].x - c[j].x, c[j + 1].y - c[j].y);
          if (segDist <= d || j === c.length - 2) {
            const frac = d > 0 ? Math.min(1.0, Math.max(0.0, segDist / d)) : 0.0;
            pt = {
              x: c[j].x + frac * (c[j + 1].x - c[j].x),
              y: c[j].y + frac * (c[j + 1].y - c[j].y),
              contourIndex: cIdx,
            };
            break;
          }
          segDist -= d;
        }
        break;
      }
      accumulated += cLen;
    }

    if (pt) {
      sampled.push({
        x: ((pt.x - midX) / extent) * scaleMeters,
        y: (-(pt.y - midY) / extent) * scaleMeters,
        contourIndex: pt.contourIndex ?? 0,
      });
    }
  }

  return sampled;
}

export const BUILTIN_SVGS = {
  dandiya_sticks: `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100" width="100" height="100">
  <path d="M 15 85 L 85 15 M 15 15 L 85 85" stroke="currentColor" stroke-width="4" stroke-linecap="round" />
</svg>`,
  diya_flame: `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100" width="100" height="100">
  <path d="M 20 60 Q 50 90 80 60 Q 50 70 20 60 Z" fill="none" stroke="currentColor" stroke-width="3" />
  <path d="M 50 55 Q 35 40 50 15 Q 65 40 50 55 Z" fill="none" stroke="currentColor" stroke-width="3" />
</svg>`,
};

/**
 * Converts raw SVG text into a 3D formation point cloud [ {x, y, z}, ... ].
 * Enforces canonical 30.0m site limits, altitude ceiling (120m), and design spacing (2.0m).
 */
export function svgToFormation(svgTextOrKey, n, { scale = 20.0, altitude = 15.0, plane = 'XY' } = {}) {
  const svgText = (typeof svgTextOrKey === 'string' && BUILTIN_SVGS[svgTextOrKey]) || svgTextOrKey;
  const contours = parseSvgToContours(svgText);

  let minX = Infinity, maxX = -Infinity, minY = Infinity, maxY = -Infinity;
  for (const c of contours) {
    for (const p of c) {
      if (p.x < minX) minX = p.x;
      if (p.x > maxX) maxX = p.x;
      if (p.y < minY) minY = p.y;
      if (p.y > maxY) maxY = p.y;
    }
  }
  const extent = Math.max(maxX - minX, maxY - minY, 1e-4);
  let totalLen = 0;
  for (const c of contours) {
    for (let j = 0; j < c.length - 1; j++) {
      totalLen += Math.hypot(c[j + 1].x - c[j].x, c[j + 1].y - c[j].y);
    }
  }
  const normalizedPerimeter = totalLen / extent;
  const minRequiredScale = ((n * MIN_DESIGN_SPACING_M) / normalizedPerimeter) * 1.05;

  let effectiveScale = Math.max(scale, minRequiredScale);

  if (effectiveScale > SITE_RADIUS_M * 2) {
    throw new Error(
      `SVG formation for ${n} drones requires ${effectiveScale.toFixed(1)}m span, exceeding site limits (${SITE_RADIUS_M}m radius).`
    );
  }

  const projectPoints = (curScale) => {
    const pts2d = sampleSvgContours(contours, n, curScale);
    if (pts2d.length === 0) return [];
    const minPy = Math.min(...pts2d.map((p) => p.y));
    const pts3d = [];
    const depthLayers = [0, 2.5, -2.5, 5.0, -5.0, 7.5, -7.5];

    for (let i = 0; i < pts2d.length; i++) {
      const p = pts2d[i];
      const baseDepth = contours.length > 1 ? (p.contourIndex - (contours.length - 1) / 2) * 2.5 : 0.0;
      let x = Number(p.x.toFixed(3));
      let y = 0;
      let z = 0;

      if (plane === 'XZ') {
        x = Number(p.x.toFixed(3));
        y = Number(p.y.toFixed(3));
        let chosenAltOffset = baseDepth;
        for (const dl of depthLayers) {
          const testZ = Number((-altitude + baseDepth + dl).toFixed(3));
          let ok = true;
          for (const other of pts3d) {
            if (Math.hypot(x - other.x, y - other.y, testZ - other.z) < MIN_DESIGN_SPACING_M - 1e-4) {
              ok = false;
              break;
            }
          }
          if (ok) {
            chosenAltOffset = baseDepth + dl;
            break;
          }
        }
        z = Number((-altitude + chosenAltOffset).toFixed(3));
      } else {
        z = Number((-altitude - (p.y - minPy)).toFixed(3));
        let chosenY = baseDepth;
        for (const dl of depthLayers) {
          const testY = Number((baseDepth + dl).toFixed(3));
          let ok = true;
          for (const other of pts3d) {
            if (Math.hypot(x - other.x, testY - other.y, z - other.z) < MIN_DESIGN_SPACING_M - 1e-4) {
              ok = false;
              break;
            }
          }
          if (ok) {
            chosenY = testY;
            break;
          }
        }
        y = chosenY;
      }
      pts3d.push({ x, y, z });
    }
    return pts3d;
  };

  let pts3d = projectPoints(effectiveScale);

  if (n > 1) {
    let m = Infinity;
    for (let i = 0; i < pts3d.length; i++) {
      for (let j = i + 1; j < pts3d.length; j++) {
        const d = Math.hypot(pts3d[i].x - pts3d[j].x, pts3d[i].y - pts3d[j].y, pts3d[i].z - pts3d[j].z);
        if (d < m) m = d;
      }
    }
    if (m < MIN_DESIGN_SPACING_M && m > 1e-4) {
      const boost = (MIN_DESIGN_SPACING_M / m) * 1.05;
      effectiveScale *= boost;
      if (effectiveScale > SITE_RADIUS_M * 2) {
        throw new Error(
          `SVG formation for ${n} drones requires ${effectiveScale.toFixed(1)}m span to maintain ${MIN_DESIGN_SPACING_M}m spacing, exceeding site limits (${SITE_RADIUS_M}m radius).`
        );
      }
      pts3d = projectPoints(effectiveScale);
    }
  }

  // Authoritative post-checks: site boundary, altitude ceiling, and design spacing
  for (const p of pts3d) {
    const reach = Math.hypot(p.x, p.y);
    if (reach > SITE_RADIUS_M + 1e-6) {
      throw new Error(`SVG formation point exceeds site radius (${reach.toFixed(2)}m > ${SITE_RADIUS_M}m).`);
    }
    const alt = -p.z; // NED: -z is height above ground
    if (alt < 0 || alt > MAX_ALTITUDE_M) {
      throw new Error(`SVG formation altitude (${alt.toFixed(2)}m) exceeds bounds (0 - ${MAX_ALTITUDE_M}m).`);
    }
  }

  return pts3d;
}
