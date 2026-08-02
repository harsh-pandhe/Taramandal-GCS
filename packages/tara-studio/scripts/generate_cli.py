#!/usr/bin/env python3
"""
Taramandal Studio - Command Line Interface (MVP)
Generates and validates choreography files from the terminal.
"""

import argparse
import sys
import os

# Put parent directory in path to allow imports from studio
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from studio.generator import generate_circle, generate_helix, generate_heart, generate_cube
from studio.validator import validate_choreography
from studio.exporter import export_csv

def main():
    parser = argparse.ArgumentParser(description="Taramandal Studio Choreography Engine MVP CLI")
    parser.add_argument(
        "-s", "--shape", 
        choices=["circle", "helix", "heart", "cube"], 
        default="heart", 
        help="Target choreography shape geometry"
    )
    parser.add_argument(
        "-n", "--num-drones", 
        type=int, 
        default=4, 
        help="Number of drones in the swarm (1-10)"
    )
    parser.add_argument(
        "-d", "--duration", 
        type=float, 
        default=30.0, 
        help="Show duration in seconds"
    )
    parser.add_argument(
        "-o", "--output", 
        type=str, 
        default="choreography.csv", 
        help="Output CSV file path"
    )
    parser.add_argument(
        "--max-vel", 
        type=float, 
        default=6.0, 
        help="Maximum velocity limit in m/s (default: 6.0)"
    )
    parser.add_argument(
        "--max-accel", 
        type=float, 
        default=50.0, 
        help="Maximum acceleration limit in m/s2 (default: 50.0)"
    )
    parser.add_argument(
        "--max-yaw", 
        type=float, 
        default=270.0, 
        help="Maximum yaw rate limit in deg/s (default: 270.0)"
    )
    
    args = parser.parse_args()
    
    print(f"Generating '{args.shape}' choreography for {args.num_drones} drones...")
    
    # 1. Call Shape Generator
    if args.shape == "circle":
        choreo = generate_circle(args.num_drones, args.duration, 2.0)
    elif args.shape == "helix":
        choreo = generate_helix(args.num_drones, args.duration, 2.0)
    elif args.shape == "heart":
        choreo = generate_heart(args.num_drones, args.duration, 2.0)
    elif args.shape == "cube":
        choreo = generate_cube(args.num_drones, args.duration, 2.0)
    else:
        print(f"Error: Unknown shape: {args.shape}")
        sys.exit(1)
        
    # 2. Run 4D Safety Check
    report = validate_choreography(
        choreo, 
        safe_dist_threshold=0.8,
        max_velocity=args.max_vel,
        max_accel=args.max_accel,
        max_yaw_rate=args.max_yaw
    )
    
    print("\nSafety Validation Report:")
    print(f"  Status: {report['status']}")
    print(f"  Summary: {report['summary']}")
    print("  Metrics:")
    print(f"    Min Separation: {report['metrics']['min_distance_m']} m")
    print(f"    Max Velocity: {report['metrics']['max_velocity_m_s']} m/s")
    print(f"    Max Acceleration: {report['metrics']['max_acceleration_m_s2']} m/s²")
    print(f"    Max Yaw Rate: {report['metrics']['max_yaw_rate_deg_s']} °/s")
    
    # Determine overall status
    if report["status"] == "FAILED":
        print("\n❌ Warning: Choreography failed safety constraints. Exiting without export.")
        # Print proximity violations specifically
        prox_violations = report["violations"]["proximity"]
        if prox_violations:
            print(f"  Proximity Violations count: {len(prox_violations)}")
            for v in prox_violations[:3]:
                print(f"    t={v['time']}s: Drone {v['drones'][0]} and Drone {v['drones'][1]} too close ({v['distance']}m)")
        sys.exit(1)
        
    # 3. Export CSV
    csv_content = export_csv(choreo)
    with open(args.output, "w") as f:
        f.write(csv_content)
        
    print(f"\n✅ Safe trajectory exported successfully to: {args.output}")

if __name__ == "__main__":
    main()
