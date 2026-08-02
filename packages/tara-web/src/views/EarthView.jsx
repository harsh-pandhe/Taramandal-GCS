import React, { useEffect, useRef } from 'react';
import * as Cesium from 'cesium';
import 'cesium/Build/Cesium/Widgets/widgets.css';
import { launchGrid, nedToGeodetic } from '../geo';

// Georeferenced 3D globe: the real venue, launch pads at their true lat/lon,
// the geofence as a translucent cylinder, and live drones as a
// PointPrimitiveCollection (one draw call regardless of drone count — this
// is what lets it scale to hundreds of live points).
//
// Deliberately uses raw Cesium (not a React wrapper like resium) — Cesium's
// object model doesn't map cleanly onto React's render cycle anyway (a
// Viewer owns a scene graph it mutates imperatively), so this component
// creates one Viewer imperatively in a ref'd container and pushes telemetry
// updates into it directly, rather than fighting a wrapper's abstraction.
//
// No Cesium ion account/token needed: imagery comes from OpenStreetMap and
// terrain is the plain WGS84 ellipsoid (both free, no token), so
// baseLayerPicker (which otherwise offers ion-hosted layers) is disabled.

const OSM_URL = 'https://tile.openstreetmap.org/';

function statusColor(drone) {
  if (!drone || !drone.connected) return Cesium.Color.GRAY.withAlpha(0.6);
  if (drone.armed) return Cesium.Color.fromCssColorString('#fbbf24'); // warning/amber — armed
  return Cesium.Color.fromCssColorString('#34d399'); // success/green — connected, on the ground
}

export default function EarthView({ telemetry, venueLat = 12.9716, venueLon = 77.5946, geofenceRadiusM = 30.0 }) {
  const containerRef = useRef(null);
  const viewerRef = useRef(null);
  const dronePointsRef = useRef(null);
  const dronePointById = useRef(new Map());
  const padEntitiesRef = useRef([]);
  const padCountRef = useRef(-1);

  // One-time Viewer setup; torn down on unmount so re-toggling the tab
  // doesn't leak WebGL contexts (a real, easy-to-hit Cesium footgun).
  useEffect(() => {
    if (!containerRef.current || viewerRef.current) return;

    const viewer = new Cesium.Viewer(containerRef.current, {
      imageryProvider: new Cesium.OpenStreetMapImageryProvider({ url: OSM_URL }),
      terrainProvider: new Cesium.EllipsoidTerrainProvider(),
      baseLayerPicker: false,
      geocoder: false,
      navigationHelpButton: false,
      infoBox: false,
      selectionIndicator: false,
      sceneModePicker: true,
      timeline: true,
      animation: true,
    });
    viewer.scene.globe.enableLighting = true;

    viewer.entities.add({
      name: 'Geofence',
      position: Cesium.Cartesian3.fromDegrees(venueLon, venueLat, geofenceRadiusM),
      cylinder: {
        length: geofenceRadiusM * 2,
        topRadius: geofenceRadiusM,
        bottomRadius: geofenceRadiusM,
        material: Cesium.Color.fromCssColorString('#38bdf8').withAlpha(0.08),
        outline: true,
        outlineColor: Cesium.Color.fromCssColorString('#38bdf8').withAlpha(0.5),
      },
    });

    dronePointsRef.current = viewer.scene.primitives.add(new Cesium.PointPrimitiveCollection());

    viewer.camera.flyTo({
      destination: Cesium.Cartesian3.fromDegrees(venueLon, venueLat, geofenceRadiusM * 6),
      duration: 0,
    });

    viewerRef.current = viewer;
    return () => {
      viewer.destroy();
      viewerRef.current = null;
      dronePointsRef.current = null;
      dronePointById.current.clear();
      padEntitiesRef.current = [];
      padCountRef.current = -1;
    };
  }, [venueLat, venueLon, geofenceRadiusM]);

  // Launch pads: redrawn only when the drone count actually changes, not on
  // every telemetry tick — they're static ground markers, not live state.
  useEffect(() => {
    const viewer = viewerRef.current;
    if (!viewer) return;
    const n = Object.keys(telemetry).length;
    if (n === padCountRef.current) return;
    padCountRef.current = n;

    padEntitiesRef.current.forEach((e) => viewer.entities.remove(e));
    padEntitiesRef.current = launchGrid(n).map(([x, y], id) => {
      const [lat, lon] = nedToGeodetic(x, y, venueLat, venueLon);
      return viewer.entities.add({
        name: `Pad ${id}`,
        position: Cesium.Cartesian3.fromDegrees(lon, lat, 0),
        point: { pixelSize: 6, color: Cesium.Color.fromCssColorString('#5b6577'), outlineColor: Cesium.Color.BLACK, outlineWidth: 1 },
      });
    });
  }, [Object.keys(telemetry).length, venueLat, venueLon]);

  // Live drone positions/status — the fast-channel fields update at 20Hz.
  useEffect(() => {
    const points = dronePointsRef.current;
    if (!points) return;
    Object.entries(telemetry).forEach(([id, drone]) => {
      if (drone.fleet_x == null) return;
      const [lat, lon] = nedToGeodetic(drone.fleet_x, drone.fleet_y, venueLat, venueLon);
      const height = Math.max(0, drone.fleet_z || 0); // fleet_z is altitude, positive-up
      const position = Cesium.Cartesian3.fromDegrees(lon, lat, height);
      const color = statusColor(drone);
      let point = dronePointById.current.get(id);
      if (!point) {
        point = points.add({ position, color, pixelSize: 12, outlineColor: Cesium.Color.BLACK, outlineWidth: 1 });
        dronePointById.current.set(id, point);
      } else {
        point.position = position;
        point.color = color;
      }
    });
  }, [telemetry, venueLat, venueLon]);

  return <div ref={containerRef} className="earth-view-container" />;
}
