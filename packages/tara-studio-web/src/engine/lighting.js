// LED choreography — computes a drone's colour at an instant in the show.
//
// The same function drives the live 3D preview and the baked export, so what a
// designer sees in the viewport is what the fleet flies.

import * as THREE from 'three';

export const LIGHT_EFFECTS = [
  { value: 'perDrone', label: 'Per-Drone Palette' },
  { value: 'solid', label: 'Solid Colour' },
  { value: 'gradient', label: 'Fleet Gradient' },
  { value: 'rainbow', label: 'Rainbow Sweep' },
  { value: 'pulse', label: 'Beat Pulse (music)' },
];

/**
 * A drone's LED colour at time `t`.
 *
 * @param {number} idx      drone index
 * @param {number} t        show time (s)
 * @param {object} opts
 * @param {string} opts.effect       one of LIGHT_EFFECTS' values
 * @param {string} opts.baseColor    hex, used by solid/gradient
 * @param {Array}  opts.dronesList   fleet config, for per-drone palette
 * @param {number} opts.numDrones
 * @param {number} opts.duration     show length, for the rainbow sweep
 * @param {number} opts.audioVolume  0..1, drives the beat pulse
 * @returns {THREE.Color}
 */
export const droneColorAt = (idx, t, opts) => {
  const {
    effect = 'perDrone', baseColor = '#38bdf8', dronesList = [],
    numDrones = 1, duration = 1, audioVolume = 0,
  } = opts || {};

  const c = new THREE.Color();
  const n = Math.max(1, numDrones);
  const cfg = dronesList.find((d) => d.id === idx);
  const perDroneHex = cfg ? cfg.colorHex : baseColor;

  switch (effect) {
    case 'solid':
      c.set(baseColor);
      break;
    case 'rainbow': {
      const hue = ((((t / (duration || 1)) + idx / n) % 1) + 1) % 1;
      c.setHSL(hue, 0.85, 0.55);
      break;
    }
    case 'gradient': {
      const base = new THREE.Color(baseColor);
      const hsl = {};
      base.getHSL(hsl);
      c.setHSL((hsl.h + (idx / n) * 0.5) % 1, Math.max(0.6, hsl.s), 0.55);
      break;
    }
    case 'pulse': {
      c.set(perDroneHex);
      const beat = audioVolume || (0.5 + 0.5 * Math.sin(t * 3));
      c.multiplyScalar(Math.min(1.6, 0.55 + beat));
      break;
    }
    case 'perDrone':
    default:
      c.set(perDroneHex);
      break;
  }
  return c;
};

/**
 * Bakes the live LED colour into every waypoint (r,g,b 0-255) so an exported
 * show carries its light choreography, not just position.
 */
export const buildColoredChoreo = (choreoData, opts) => {
  if (!choreoData) return choreoData;
  return {
    ...choreoData,
    drones: choreoData.drones.map((d, i) => ({
      ...d,
      waypoints: d.waypoints.map((wp) => {
        const c = droneColorAt(i, wp.time, opts);
        return {
          ...wp,
          r: Math.round(c.r * 255),
          g: Math.round(c.g * 255),
          b: Math.round(c.b * 255),
        };
      }),
    })),
  };
};
