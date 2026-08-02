"""
Taramandal Studio - Trajectory Exporter (CSV, JSON, KML)
"""

import json
import csv
import io
import math

def export_json(choreo_data: dict) -> str:
    return json.dumps(choreo_data, indent=2)

def export_csv(choreo_data: dict) -> str:
    output = io.StringIO()
    writer = csv.writer(output)
    # Include LED colour columns (r,g,b 0-255) when the choreography carries
    # light data, so exported shows keep their colour choreography.
    has_color = any(
        "r" in wp
        for drone in choreo_data.get("drones", [])
        for wp in drone.get("waypoints", [])
    )
    header = ["drone_id", "time", "x", "y", "z", "yaw"]
    if has_color:
        header += ["r", "g", "b"]
    writer.writerow(header)

    for drone in choreo_data.get("drones", []):
        d_id = drone["id"]
        for wp in drone.get("waypoints", []):
            row = [d_id, wp["time"], wp["x"], wp["y"], wp["z"], wp["yaw"]]
            if has_color:
                row += [wp.get("r", 255), wp.get("g", 255), wp.get("b", 255)]
            writer.writerow(row)

    return output.getvalue()

def export_kml(choreo_data: dict, home_lat: float = 12.9716, home_lon: float = 77.5946) -> str:
    """
    Exports trajectories to Google Earth KML format using relativeToGround coordinates.
    Local (x, y) coordinates map to North/East GPS offsets.
    """
    # Earth radius
    R = 6378137.0
    
    kml_parts = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<kml xmlns="http://www.opengis.net/kml/2.2">',
        '  <Document>',
        '    <name>Taramandal Swarm Choreography Mockup</name>',
        '    <description>3D flight paths of the Taramandal Drone Show</description>',
        '    <Style id="path_style">',
        '      <LineStyle>',
        '        <color>ff00ff00</color> <!-- Green -->',
        '        <width>3</width>',
        '      </LineStyle>',
        '      <PolyStyle>',
        '        <color>2200ff00</color> <!-- Semi-transparent ground wall -->',
        '      </PolyStyle>',
        '    </Style>'
    ]
    
    for drone in choreo_data.get("drones", []):
        d_id = drone["id"]
        wps = drone.get("waypoints", [])
        
        coord_strings = []
        for wp in wps:
            # Convert local North (x) and East (y) meters to Lat/Lon offsets
            dlat = wp["x"] / R
            dlon = wp["y"] / (R * math.cos(math.radians(home_lat)))
            
            lat = home_lat + math.degrees(dlat)
            lon = home_lon + math.degrees(dlon)
            
            # Local Z is negative Down, so height above ground is -z
            height = max(0.0, -wp["z"])
            
            coord_strings.append(f"{lon:.8f},{lat:.8f},{height:.2f}")
            
        coords_block = " ".join(coord_strings)
        
        kml_parts.extend([
            f'    <Placemark>',
            f'      <name>Drone {d_id:02d}</name>',
            f'      <styleUrl>#path_style</styleUrl>',
            f'      <LineString>',
            f'        <extrude>1</extrude>',
            f'        <tessellate>1</tessellate>',
            f'        <altitudeMode>relativeToGround</altitudeMode>',
            f'        <coordinates>{coords_block}</coordinates>',
            f'      </LineString>',
            f'    </Placemark>'
        ])
        
    kml_parts.extend([
        '  </Document>',
        '</kml>'
    ])
    
    return "\n".join(kml_parts)
