from fastapi import FastAPI
import routers.kachaka as kachaka
import routers.tasks as tasks
import routers.settings as settings_router
import routers.bio_sensor as bio_sensor
import routers.buttons as buttons_router
from fastapi.staticfiles import StaticFiles
from contextlib import asynccontextmanager
import asyncio
import logging
from logging.handlers import RotatingFileHandler
import os
import sys
import traceback

from services.task_runtime import (
    engines, task_queues, task_worker, TaskEngine
)
from services.scheduler import scheduler_service
from services import action_registry, button_db
from services.zigbee_mqtt import ZigbeeMQTT
from services.button_manager import ButtonManager
from dependencies import get_fleet, get_bio_sensor_client

# ---------------------------------------------------------------------------
# Logging setup: stdout + per-module log files under <project_root>/data/logs/
# ---------------------------------------------------------------------------
_LOG_FORMAT = "%(asctime)s - %(name)s - %(levelname)s - %(message)s"

def _setup_logging():
    log_dir = os.path.join(get_project_root(), "data", "logs")
    os.makedirs(log_dir, exist_ok=True)

    formatter = logging.Formatter(_LOG_FORMAT)

    # stdout – keeps docker compose logs readable
    stdout_h = logging.StreamHandler(sys.stdout)
    stdout_h.setFormatter(formatter)

    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.addHandler(stdout_h)

    def _file_handler(filename):
        h = RotatingFileHandler(
            os.path.join(log_dir, filename),
            maxBytes=5 * 1024 * 1024,   # 5 MB
            backupCount=3,
        )
        h.setFormatter(formatter)
        return h

    # Route loggers → separate files
    # app.log   : main lifecycle, fleet/robot, telegram, settings, utils
    # task.log  : patrol task execution
    # sensor.log: bio-sensor MQTT data
    # scheduler.log: cron-style scheduler
    routing = {
        "app.log": [
            "bio_patrol", "services.fleet_api",
            "services.telegram_service", "routers.settings", "utils",
        ],
        "task.log": [
            "kachaka", "routers.tasks", "routers.kachaka",
        ],
        "sensor.log": [
            "BioSensorMQTTClient", "routers.bio_sensor",
        ],
        "scheduler.log": [
            "services.scheduler",
        ],
    }
    for filename, names in routing.items():
        fh = _file_handler(filename)
        for name in names:
            logging.getLogger(name).addHandler(fh)

def get_project_root():
    """Get project root directory. From src/backend/main.py → up 3 levels to project root."""
    return os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

def get_resource_path(relative_path):
    """Get absolute path to resource relative to project root."""
    return os.path.join(get_project_root(), relative_path)

_setup_logging()
logger = logging.getLogger("bio_patrol.main")

@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Starting application lifespan...")
    bio_sensor_client = None
    zigbee_mqtt = None
    button_manager = None
    try:
        # Bio-sensor MQTT client
        from settings.config import get_runtime_settings
        cfg = get_runtime_settings()

        if cfg.get("mqtt_enabled"):
            try:
                bio_sensor_client = get_bio_sensor_client()
                bio_sensor_client.start()
                logger.info("Bio-sensor MQTT client started successfully")
            except Exception as e:
                logger.error(f"Failed to start bio-sensor MQTT client: {e}")
        else:
            logger.info("Bio-sensor MQTT client disabled (mqtt_enabled=false)")

        # Single robot registration from runtime settings
        robot_ip = cfg.get("robot_ip", "192.168.204.37:26400")
        robot_id = "kachaka"

        fleet_client = get_fleet()
        try:
            result = await fleet_client.register_robot(robot_id, robot_ip, "Kachaka Care")
            if not result.get("ok"):
                raise Exception(f"Registration failed: {result.get('error', 'unknown')}")
            engines[robot_id] = TaskEngine(fleet_client, robot_id)
            task_queues[robot_id] = asyncio.Queue()
            asyncio.create_task(task_worker(robot_id))
            logger.info(f"Robot '{robot_id}' registered at {robot_ip}")
        except Exception as e:
            logger.error(f"Failed to register robot '{robot_id}': {e}")
            logger.info(f"Continuing with graceful degradation for robot {robot_id}")

        # Start task scheduler
        logger.info("Starting task scheduler...")
        await scheduler_service.start()

        try:
            action_registry.init_default_actions()
            button_db.init_schema()
            button_db.seed_actions([a["key"] for a in action_registry.list_actions()])
            if cfg.get("zigbee_enabled", True):
                zigbee_mqtt = ZigbeeMQTT(
                    host=cfg.get("zigbee_mqtt_host", "mqtt-broker"),
                    port=int(cfg.get("zigbee_mqtt_port", 1883)),
                )
                button_manager = ButtonManager(zigbee_mqtt)
                zigbee_mqtt.set_handler(button_manager.handle_event)
                await zigbee_mqtt.start()
                buttons_router.set_manager(button_manager)
                logger.info("Zigbee button bindings initialised")
            else:
                logger.info("Zigbee bindings disabled (zigbee_enabled=false)")
        except Exception as e:
            logger.error(f"Failed to initialise zigbee button bindings: {e}")

        yield

        # Cleanup
        if zigbee_mqtt:
            try:
                await zigbee_mqtt.stop()
            except Exception:
                pass
        if bio_sensor_client:
            bio_sensor_client.stop()
        await scheduler_service.stop()
        try:
            await fleet_client.unregister_robot(robot_id)
        except Exception:
            pass
        logger.info("Application shutdown: Clean up completed.")
    except Exception as e:
        logger.error(f"Error during application startup: {e}")
        logger.error(traceback.format_exc())
        raise

app = FastAPI(
    title="Bio Patrol",
    description="Bio-sensor patrol system",
    version="1.0.0",
    lifespan=lifespan
)

# Include routers (before static files mount so API routes take priority)
app.include_router(tasks.router)
app.include_router(kachaka.router)
app.include_router(settings_router.router)
app.include_router(bio_sensor.router)
app.include_router(buttons_router.router)

frontend_path = get_resource_path("src/frontend")
if os.path.exists(frontend_path):
    app.mount("/", StaticFiles(directory=frontend_path, html=True), name="ui")
else:
    logger.warning(f"Frontend directory not found at {frontend_path}, UI will not be available")
