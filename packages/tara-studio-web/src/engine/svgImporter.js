/**
 * Deterministic, safe SVG-to-formation parser and resampler (issue #129).
 *
 * Converts safe SVG path outlines into exactly N drone positions meeting the
 * 2.0 m minimum design spacing requirement, without executing scripts or
 * embedding untrusted markup into the DOM.
 */

import { ShowRefusalError } from './showErrors.js';

const MAX_SVG_BYTES = 500_000;
export const MIN_DESIGN_SPACING_M = 2.0;
export const SITE_RADIUS_M = 30.0;
export const MAX_ALTITUDE_M = 120.0;

// Known over-rejection (issue #122/#146 review, documented rather than fixed -
// relaxing it is a grammar change, out of scope for a fail-closed importer):
// `/<!DOCTYPE/i` and `/SYSTEM/i` match ANY occurrence of that text, including
// inside a `<title>`/`<desc>` string that has nothing to do with XML's actual
// DOCTYPE/external-entity syntax (e.g. a title literally containing the word
// "System"). Accepted as a safe false-positive: it can only ever reject a
// safe file, never admit an unsafe one.
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

export const IDENTITY_MATRIX = Object.freeze([1, 0, 0, 1, 0, 0]);

export function applyMatrix(m, pt) {
  return {
    x: m[0] * pt.x + m[2] * pt.y + m[4],
    y: m[1] * pt.x + m[3] * pt.y + m[5],
  };
}

export function multiplyMatrices(m1, m2) {
  return [
    m1[0] * m2[0] + m1[2] * m2[1],
    m1[1] * m2[0] + m1[3] * m2[1],
    m1[0] * m2[2] + m1[2] * m2[3],
    m1[1] * m2[2] + m1[3] * m2[3],
    m1[0] * m2[4] + m1[2] * m2[5] + m1[4],
    m1[1] * m2[4] + m1[3] * m2[5] + m1[5],
  ];
}

/**
 * Parses 2D transforms (translate, scale, rotate, matrix).
 * Rejects unsupported (e.g. skewX, skewY) or malformed transforms explicitly.
 */
export function parseTransform(transformStr) {
  if (!transformStr || !transformStr.trim()) {
    const fn = (pt) => pt;
    fn.matrix = [...IDENTITY_MATRIX];
    return fn;
  }
  const str = transformStr.trim();

  // Explicitly reject unsupported transform types
  const unsupported = /(?:skewX|skewY)\s*\(/i;
  if (unsupported.test(str)) {
    throw new Error(`Unsupported SVG transform: '${transformStr}'. Only translate, scale, rotate, and matrix transforms are supported.`);
  }

  const transformRegex = /(translate|scale|rotate|matrix)\s*\(\s*([^)]*)\s*\)/gi;
  let curMatrix = [...IDENTITY_MATRIX];
  let lastIndex = 0;
  let match;

  while ((match = transformRegex.exec(str)) !== null) {
    const prefix = str.slice(lastIndex, match.index).trim();
    if (prefix && prefix !== ',') {
      throw new Error(`Malformed SVG transform: unrecognized syntax near '${prefix}'.`);
    }
    lastIndex = transformRegex.lastIndex;

    const op = match[1].toLowerCase();
    const argsStr = match[2].trim();
    const rawTokens = argsStr.split(/[\s,]+/).filter(Boolean);
    const args = rawTokens.map(Number);
    if (rawTokens.length === 0 || args.some((n) => isNaN(n))) {
      throw new Error(`Malformed SVG transform parameters in '${match[0]}'.`);
    }

    let opMatrix;
    if (op === 'translate') {
      if (args.length < 1 || args.length > 2) {
        throw new Error(`Invalid parameter count for translate() in '${match[0]}'. Expected 1 or 2.`);
      }
      const tx = args[0];
      const ty = args.length > 1 ? args[1] : 0;
      opMatrix = [1, 0, 0, 1, tx, ty];
    } else if (op === 'scale') {
      if (args.length < 1 || args.length > 2) {
        throw new Error(`Invalid parameter count for scale() in '${match[0]}'. Expected 1 or 2.`);
      }
      const sx = args[0];
      const sy = args.length > 1 ? args[1] : sx;
      opMatrix = [sx, 0, 0, sy, 0, 0];
    } else if (op === 'rotate') {
      if (args.length !== 1 && args.length !== 3) {
        throw new Error(`Invalid parameter count for rotate() in '${match[0]}'. Expected 1 or 3.`);
      }
      const angle = args[0];
      const rad = (angle * Math.PI) / 180;
      const cos = Math.cos(rad);
      const sin = Math.sin(rad);
      if (args.length === 3) {
        const cx = args[1], cy = args[2];
        const toOrigin = [1, 0, 0, 1, cx, cy];
        const rot = [cos, sin, -sin, cos, 0, 0];
        const fromOrigin = [1, 0, 0, 1, -cx, -cy];
        opMatrix = multiplyMatrices(multiplyMatrices(toOrigin, rot), fromOrigin);
      } else {
        opMatrix = [cos, sin, -sin, cos, 0, 0];
      }
    } else if (op === 'matrix') {
      if (args.length !== 6) {
        throw new Error(`Invalid parameter count for matrix() in '${match[0]}'. Expected 6 numeric values.`);
      }
      opMatrix = args;
    }

    curMatrix = multiplyMatrices(curMatrix, opMatrix);
  }

  const trailing = str.slice(lastIndex).trim();
  if (trailing) {
    throw new Error(`Malformed or unsupported SVG transform: '${trailing}'.`);
  }

  const fn = (pt) => applyMatrix(curMatrix, pt);
  fn.matrix = curMatrix;
  return fn;
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

    // 'Z' takes no parameters, so (unlike M/L/H/V/C/Q) it cannot be
    // implicitly repeated by a following bare number with no command
    // letter (self-review, pre-merge). Without this check, a bare number
    // right after 'Z' left `cmd` unchanged and `i` never advanced past it -
    // the Z branch re-ran on the same token forever, hanging the thread on
    // input like 'M0 0 L1 1 Z 2 2' instead of rejecting it.
    if (upper === 'Z' && t.type !== 'cmd') {
      throw new Error(`Malformed SVG path data: 'Z' does not take parameters and cannot be implicitly repeated.`);
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
      // Per the SVG spec, a command right after a closepath (other than a
      // fresh M, handled separately above) continues from the subpath's
      // start point rather than beginning an empty one (self-review,
      // pre-merge: 'M0 0 L1 1 Z L2 2' previously dropped the 'L2 2' segment
      // entirely, since the following L only pushed its own endpoint onto
      // an empty curContour, leaving it at length 1 - below the >= 2
      // threshold that makes it into `contours` - and silently losing that
      // geometry with no error).
      curContour = [{ x: curX, y: curY }];
    }
  }

  if (curContour.length >= 2) {
    contours.push(curContour);
  }

  return contours;
}

function parseAttributes(attrStr) {
  const attrs = {};
  if (!attrStr) return attrs;
  const regex = /([a-zA-Z0-9:-]+)\s*=\s*(?:'([^']*)'|"([^"]*)"|([^\s>]+))/gi;
  let m;
  while ((m = regex.exec(attrStr)) !== null) {
    attrs[m[1].toLowerCase()] = m[2] !== undefined ? m[2] : (m[3] !== undefined ? m[3] : m[4]);
  }
  return attrs;
}

/**
 * True when an element carries any of the standard ways SVG hides content
 * from rendering without removing it (issue #122/#146 review): the
 * `display`/`visibility`/`opacity` attributes, or the same properties in an
 * inline `style`. An importer that ignores these would silently import
 * geometry that no renderer ever draws.
 */
function isHiddenAttrs(attrs) {
  if (attrs.display !== undefined && attrs.display.trim().toLowerCase() === 'none') return true;
  if (attrs.visibility !== undefined && attrs.visibility.trim().toLowerCase() === 'hidden') return true;
  if (attrs.opacity !== undefined && Number(attrs.opacity) === 0) return true;
  if (attrs.style) {
    const style = attrs.style.toLowerCase();
    if (/display\s*:\s*none\b/.test(style)) return true;
    if (/visibility\s*:\s*hidden\b/.test(style)) return true;
    // Word boundary before "opacity" (self-review, pre-merge): without it,
    // this also matched inside stroke-opacity:0 or fill-opacity:0, hiding
    // the whole element even when its fill (or stroke) was still visible.
    if (/(?<![\w-])opacity\s*:\s*0(?:\.0+)?\b/.test(style)) return true;
  }
  return false;
}

/**
 * Extracts 2D contours from safe SVG elements, applying nested group transforms and viewBox mapping.
 */
export function parseSvgToContours(svgText) {
  const clean = sanitizeSvg(svgText);
  const vb = validateViewBox(clean);
  const contours = [];

  // Match raw path string if no XML tag
  if (clean.trim().startsWith('M') || clean.trim().startsWith('m')) {
    const sub = parsePathCommands(clean.trim());
    for (const pts of sub) {
      if (pts.length >= 2) contours.push(pts);
    }
    if (contours.length === 0) {
      throw new Error('SVG contains no valid drawable paths or polygons.');
    }
    return contours;
  }

  // Parse root <svg> attributes for width, height, preserveAspectRatio
  const svgOpenMatch = /<svg\b([^>]*)>/i.exec(clean);
  let svgW = null, svgH = null, preserveAspectRatio = 'xmidymid meet';
  if (svgOpenMatch) {
    const svgAttrs = parseAttributes(svgOpenMatch[1]);
    if (svgAttrs.width) {
      const w = parseFloat(svgAttrs.width);
      if (!isNaN(w) && w > 0) svgW = w;
    }
    if (svgAttrs.height) {
      const h = parseFloat(svgAttrs.height);
      if (!isNaN(h) && h > 0) svgH = h;
    }
    if (svgAttrs.preserveaspectratio) {
      preserveAspectRatio = svgAttrs.preserveaspectratio.trim().toLowerCase();
    }
  }

  // Validate preserveAspectRatio
  if (preserveAspectRatio.includes('slice')) {
    throw new Error("Unsupported SVG preserveAspectRatio: 'slice' is not supported.");
  }
  if (preserveAspectRatio !== 'none' && !preserveAspectRatio.startsWith('xmidymid')) {
    throw new Error(`Unsupported SVG preserveAspectRatio: '${preserveAspectRatio}'.`);
  }

  // Build viewBox coordinate mapper: maps user coordinates into viewport
  const mapCoordinate = (pt) => {
    if (!vb) return pt;
    if (svgW && svgH && (vb.width > 0 && vb.height > 0)) {
      if (preserveAspectRatio === 'none') {
        const sx = svgW / vb.width;
        const sy = svgH / vb.height;
        return {
          x: (pt.x - vb.minX) * sx,
          y: (pt.y - vb.minY) * sy,
        };
      } else {
        const scale = Math.min(svgW / vb.width, svgH / vb.height);
        const offX = (svgW - vb.width * scale) / 2;
        const offY = (svgH - vb.height * scale) / 2;
        return {
          x: offX + (pt.x - vb.minX) * scale,
          y: offY + (pt.y - vb.minY) * scale,
        };
      }
    }
    return {
      x: pt.x - vb.minX,
      y: pt.y - vb.minY,
    };
  };

  // Tag regex to iterate hierarchically over elements and maintain transform stack.
  // A single greedy [^>]* captures everything up to '>', including any
  // trailing self-closing '/' - the previous ((?:\s+[^>]*?)?) shape instead
  // backtracked the lazy [^>]*? against every length the greedy \s+ gave up,
  // which is quadratic on a long unterminated tag (issue #122/#146 review:
  // 50k spaces took 0.57s, 200k took 7.9s on the main thread). Self-closing
  // is read back out of the captured attrsStr below, not out of a trailing
  // (\/?) capture group: that group could only ever be empty here, since the
  // match already succeeds with it empty and a regex only backtracks when
  // the match would otherwise fail.
  const textWithoutComments = clean.replace(/<!--[\s\S]*?-->/g, '');
  const tagRegex = /<(\/)?([a-zA-Z0-9:-]+)([^>]*)>/g;
  const transformStack = [[...IDENTITY_MATRIX]];
  const currentMatrix = () => transformStack[transformStack.length - 1];

  const SUPPORTED_DRAWABLE = new Set(['path', 'polygon', 'polyline']);
  const UNSUPPORTED_GEOMETRY = new Set(['rect', 'circle', 'ellipse', 'line', 'text', 'mesh']);
  // metadata is accepted as an inert, opaque container, same as title/desc -
  // its typical content (e.g. Inkscape/Illustrator's <rdf:RDF> license/
  // authorship blocks) uses element names outside this importer's safe
  // subset and is rejected like any other unsupported element (issue
  // #122/#146 review: documented over-rejection, not a new metadata
  // grammar - out of scope for a fail-closed importer).
  const STRUCTURAL = new Set(['svg', 'g', 'title', 'desc', 'metadata']);

  // >0 while scanning inside a <defs> block or a hidden (display:none /
  // visibility:hidden / opacity:0) subtree. <defs> content is only ever
  // drawn via <use>, which FORBIDDEN_PATTERNS bans outright, so nothing
  // inside it can ever be rendered; a hidden subtree is the same - never
  // visible, whatever element names it contains. Skipping it entirely (not
  // just the drawable children) means a hidden <rect> or other element
  // outside the safe subset doesn't get rejected either, since it has zero
  // rendering impact regardless (issue #122/#146 review).
  let suppressDepth = 0;
  let sawRootSvg = false;

  let match;
  while ((match = tagRegex.exec(textWithoutComments)) !== null) {
    const isClosing = match[1] === '/';
    const tagName = match[2].toLowerCase();
    // A greedy [^>]* always consumes a trailing '/' as part of attrsStr
    // (the match already succeeds with group 4 empty, so the engine never
    // backtracks to free it up) - self-closing has to be read back out of
    // the captured text itself, not out of a capture group that can never
    // be non-empty (2026-10-26 release review: this regression made every
    // self-closing tag act as if it had a real, never-encountered closing
    // tag - a self-closing hidden element suppressed everything after it
    // forever, and a self-closing <g transform> leaked onto every sibling).
    let attrsStr = match[3] || '';
    const trimmed = attrsStr.trimEnd();
    const isSelfClosing = trimmed.endsWith('/');
    if (isSelfClosing) attrsStr = trimmed.slice(0, -1);

    if (suppressDepth > 0) {
      if (isClosing) suppressDepth--;
      else if (!isSelfClosing) suppressDepth++;
      continue;
    }

    if (isClosing) {
      if (tagName === 'g' || tagName === 'svg') {
        if (transformStack.length > 1) {
          transformStack.pop();
        } else if (tagName === 'g') {
          throw new Error('Malformed SVG: mismatched closing </g> tag.');
        }
      }
      continue;
    }

    if (tagName === 'svg') {
      // A nested <svg transform="..."> previously mutated transformStack[0]
      // (the base transform) unconditionally, shifting every sibling parsed
      // after it; its own close also risked popping an unrelated ancestor
      // <g>'s matrix early, since nothing pushed for the nested <svg> itself.
      // Rejecting it outright is simpler and safer than scoping a second,
      // independent viewport (issue #122/#146 review).
      if (sawRootSvg) {
        throw new Error('Nested <svg> elements are not supported.');
      }
      sawRootSvg = true;
    }

    const attrs = parseAttributes(attrsStr);

    if (tagName === 'defs' || isHiddenAttrs(attrs)) {
      // Known under-import, documented rather than fixed (2026-10-26 release
      // review): real SVG lets a descendant set visibility="visible" to
      // render again inside a visibility:hidden ancestor (display:none and
      // opacity:0 have no such override). Skipping the whole subtree misses
      // that one case. Narrower than the "imports geometry that never
      // renders" defect class this importer exists to close, and visibility
      // isn't used by either bundled asset; not worth a per-descendant
      // visibility re-check for a documented-content SVG subset this small.
      if (!isSelfClosing) suppressDepth = 1;
      continue;
    }

    if (UNSUPPORTED_GEOMETRY.has(tagName)) {
      throw new Error(`Unsupported SVG element '<${tagName}>'. Supported drawable elements are <path>, <polygon>, and <polyline>.`);
    }

    if (!SUPPORTED_DRAWABLE.has(tagName) && !STRUCTURAL.has(tagName)) {
      throw new Error(`Unsupported SVG element '<${tagName}>'.`);
    }

    let elemMatrix = [...IDENTITY_MATRIX];
    if (attrs.transform) {
      const parsedFn = parseTransform(attrs.transform);
      elemMatrix = parsedFn.matrix;
    }

    if (tagName === 'g') {
      const nextMatrix = multiplyMatrices(currentMatrix(), elemMatrix);
      if (!isSelfClosing) {
        transformStack.push(nextMatrix);
      }
    } else if (tagName === 'svg') {
      if (attrs.transform) {
        transformStack[0] = multiplyMatrices(transformStack[0], elemMatrix);
      }
    } else if (tagName === 'path') {
      if (!attrs.d || !attrs.d.trim()) continue;
      const effectiveMatrix = multiplyMatrices(currentMatrix(), elemMatrix);
      const sub = parsePathCommands(attrs.d);
      for (const pts of sub) {
        if (pts.length >= 2) {
          const transformed = pts.map((p) => mapCoordinate(applyMatrix(effectiveMatrix, p)));
          contours.push(transformed);
        }
      }
    } else if (tagName === 'polygon' || tagName === 'polyline') {
      if (!attrs.points || !attrs.points.trim()) continue;
      const effectiveMatrix = multiplyMatrices(currentMatrix(), elemMatrix);
      const nums = attrs.points.trim().split(/[\s,]+/).map(Number);
      if (nums.some((n) => isNaN(n)) || nums.length < 4 || nums.length % 2 !== 0) {
        throw new Error('Malformed SVG polygon/polyline points data.');
      }
      const pts = [];
      for (let k = 0; k < nums.length - 1; k += 2) {
        const rawPt = { x: nums[k], y: nums[k + 1] };
        pts.push(mapCoordinate(applyMatrix(effectiveMatrix, rawPt)));
      }
      if (pts.length >= 2) contours.push(pts);
    }
  }

  if (contours.length === 0) {
    throw new Error('SVG contains no valid drawable paths or polygons.');
  }

  contours.viewBox = vb;
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
  if (totalLen <= 1e-6) {
    throw new Error('Degenerate SVG geometry (zero path length).');
  }
  const normalizedPerimeter = totalLen / extent;
  const minRequiredScale = ((n * MIN_DESIGN_SPACING_M) / normalizedPerimeter) * 1.05;

  let effectiveScale = Math.max(scale, minRequiredScale);

  if (effectiveScale > SITE_RADIUS_M * 2) {
    throw new ShowRefusalError(
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
        throw new ShowRefusalError(
          `SVG formation for ${n} drones requires ${effectiveScale.toFixed(1)}m span to maintain ${MIN_DESIGN_SPACING_M}m spacing, exceeding site limits (${SITE_RADIUS_M}m radius).`
        );
      }
      pts3d = projectPoints(effectiveScale);
    }
  }

  // Authoritative post-checks: finite coordinates, site boundary, altitude ceiling, and design spacing
  for (const p of pts3d) {
    // Defensive, not a refusal (plain Error, not ShowRefusalError): valid SVG
    // input should never project to a non-finite coordinate, so this guards
    // against a bug in the resampling/projection math above, the same way
    // buildSplitFormation's own "generator returned the wrong count" checks
    // guard against a bug rather than a well-formed-but-unsafe show.
    if (!Number.isFinite(p.x) || !Number.isFinite(p.y) || !Number.isFinite(p.z)) {
      throw new Error('SVG formation produced non-finite coordinates.');
    }
    const reach = Math.hypot(p.x, p.y);
    if (reach > SITE_RADIUS_M + 1e-6) {
      throw new ShowRefusalError(`SVG formation point exceeds site radius (${reach.toFixed(2)}m > ${SITE_RADIUS_M}m).`);
    }
    const alt = -p.z; // NED: -z is height above ground
    if (alt < 0 || alt > MAX_ALTITUDE_M) {
      throw new ShowRefusalError(`SVG formation altitude (${alt.toFixed(2)}m) exceeds bounds (0 - ${MAX_ALTITUDE_M}m).`);
    }
  }

  if (n > 1) {
    let finalDist = Infinity;
    for (let i = 0; i < pts3d.length; i++) {
      for (let j = i + 1; j < pts3d.length; j++) {
        const d = Math.hypot(pts3d[i].x - pts3d[j].x, pts3d[i].y - pts3d[j].y, pts3d[i].z - pts3d[j].z);
        if (d < finalDist) finalDist = d;
      }
    }
    if (finalDist < MIN_DESIGN_SPACING_M - 1e-4) {
      throw new ShowRefusalError(
        `SVG formation violates minimum design spacing: ${finalDist.toFixed(2)}m < ${MIN_DESIGN_SPACING_M}m.`
      );
    }
  }

  return pts3d;
}
