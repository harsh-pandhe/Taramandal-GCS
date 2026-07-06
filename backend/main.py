import asyncio
import logging
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, UploadFile, File, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from backend.drone_manager import DroneManager
from backend.trajectory_parser import parse_trajectory_file, parse_trajectory_bytes

# Set up logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("TaramandalGCS")

app = FastAPI(title="Taramandal Ground Control Station API")

# Enable CORS for frontend connectivity
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Connect to the 3 simulation drones by default
drone_manager = DroneManager(ports=[14540, 14541, 14542])

@app.on_event("startup")
async def startup_event():
    logger.info("Initializing Ground Control Station...")
    # Run drone manager connections in background with error handling
    async def run_connections():
        try:
            await drone_manager.connect_all()
        except Exception as e:
            logger.error(f"Failed to initialize drone connections on GCS startup: {e}")
    asyncio.create_task(run_connections())

@app.on_event("shutdown")
async def shutdown_event():
    logger.info("Shutting down Ground Control Station...")
    await drone_manager.shutdown()

@app.post("/api/launch")
async def launch_fleet():
    logger.info("API: Launching fleet...")
    # Wrap launch sequence task with error handling
    async def run_launch():
        try:
            await drone_manager.launch_sequence()
        except Exception as e:
            logger.error(f"Error during launch sequence execution: {e}")
    asyncio.create_task(run_launch())
    return {"status": "success", "message": "Launch sequence initiated."}

@app.post("/api/rtl")
async def trigger_rtl():
    logger.info("API: Failsafe RTL triggered...")
    await drone_manager.trigger_rtl()
    return {"status": "success", "message": "Emergency RTL commanded."}

@app.post("/api/land")
async def trigger_land():
    logger.info("API: Land triggered...")
    await drone_manager.trigger_land()
    return {"status": "success", "message": "Emergency landing commanded."}

@app.post("/api/upload-trajectory")
async def upload_trajectory(file: UploadFile = File(...)):
    logger.info(f"API: Ingesting trajectory file: {file.filename}")
    
    # Check filename extension
    filename = file.filename.lower()
    if not (filename.endswith('.json') or filename.endswith('.csv') or filename.endswith('.skyc')):
        raise HTTPException(status_code=400, detail="Unsupported file format. Must be .json, .csv, or .skyc")
        
    # Check file size limit (5 MB max) to prevent OOM
    MAX_FILE_SIZE = 5 * 1024 * 1024  # 5 MB
    
    content_length = file.headers.get("content-length")
    if content_length and int(content_length) > MAX_FILE_SIZE:
        raise HTTPException(status_code=400, detail="File too large. Maximum size is 5MB.")
        
    content = bytearray()
    while True:
        chunk = await file.read(8192)
        if not chunk:
            break
        content.extend(chunk)
        if len(content) > MAX_FILE_SIZE:
            raise HTTPException(status_code=400, detail="File too large. Maximum size is 5MB.")
            
    try:
        parsed_trajectory = parse_trajectory_bytes(bytes(content), file.filename)
        
        # Start playing back the trajectory
        drone_manager.start_trajectory(parsed_trajectory)
        
        # Calculate summary details for response
        summary = {}
        for drone_id, wps in parsed_trajectory.items():
            summary[str(drone_id)] = {
                "waypoints_count": len(wps),
                "duration_seconds": wps[-1]["time"] if wps else 0.0
            }
            
        return {
            "status": "success",
            "message": "Trajectory uploaded and executing.",
            "drones_summary": summary,
            "trajectory_data": parsed_trajectory
        }
    except Exception as e:
        logger.error(f"Failed parsing trajectory file: {e}")
        raise HTTPException(status_code=400, detail=str(e))

@app.post("/api/stop-trajectory")
async def stop_trajectory():
    logger.info("API: Stopping trajectory playback...")
    drone_manager.stop_trajectory()
    return {"status": "success", "message": "Trajectory playback stopped."}

@app.websocket("/ws/telemetry")
async def websocket_telemetry_endpoint(websocket: WebSocket):
    await websocket.accept()
    logger.info("WebSocket telemetry client connected.")
    try:
        while True:
            # Stream entire telemetry dictionary to the client
            await websocket.send_json(drone_manager.telemetry)
            await asyncio.sleep(0.2)  # Stream at 5Hz (every 200ms)
    except WebSocketDisconnect:
        logger.info("WebSocket telemetry client disconnected.")
    except Exception as e:
        logger.error(f"WebSocket telemetry error: {e}")
