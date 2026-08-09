"""
Utility script to generate fake vital sensor scan data for testing and verification.
This script can be run independently to populate the sensor_data.db with test data.
"""
import json
import uuid
import random
from datetime import datetime, timedelta
import os
import sys

# From src/backend/utils/ → up 4 levels to project root
project_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
# Add src/backend/ to path for imports
sys.path.append(os.path.join(project_root, "src", "backend"))

from utils.sqlite_wal import connect_db  # noqa: E402  (needs sys.path above)

def get_db_path():
    """Get the database path consistent with the project structure."""
    data_dir = os.path.join(project_root, "data")
    os.makedirs(data_dir, exist_ok=True)
    return os.path.join(data_dir, "sensor_data.db")

def init_database(db_path):
    """Initialize database with the same schema as the project."""
    conn = connect_db(db_path)
    cursor = conn.cursor()
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS sensor_scan_data (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id TEXT NOT NULL,
            location_id TEXT NOT NULL,
            timestamp TEXT NOT NULL,
            retry_count INTEGER NOT NULL,
            status INTEGER,
            bpm INTEGER,
            rpm INTEGER,
            data_json TEXT,
            is_valid BOOLEAN DEFAULT FALSE,
            details TEXT NULL 
        )
    ''')
    conn.commit()
    conn.close()

def save_scan_data(db_path, task_id, data, retry_count, is_valid, timestamp):
    """Save scan data to database."""
    conn = connect_db(db_path)
    cursor = conn.cursor()
    cursor.execute('''
        INSERT INTO sensor_scan_data 
        (task_id, location_id, timestamp, retry_count, status, bpm, rpm, data_json, is_valid, details)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
    ''', (
        task_id,
        data.get('location_id'),
        timestamp,
        retry_count,
        data.get('status'),
        data.get('bpm'),
        data.get('rpm'),
        json.dumps(data),
        is_valid,
        data.get('details'),
    ))
    conn.commit()
    conn.close()

def generate_fake_scan_tasks(db_path, num_tasks=10):
    """Generate fake sensor scan tasks with various scenarios."""
    print(f"Generating {num_tasks} fake scan tasks...")
    
    # Base time - start from 2 hours ago
    base_time = datetime.now() - timedelta(hours=2)
    
    for task_num in range(num_tasks):
        task_id = str(uuid.uuid4())
        task_base_time = base_time + timedelta(minutes=task_num * 12)
        
        # Different scenarios to simulate real-world cases
        scenarios = ['success_first', 'success_retry', 'all_fail', 'mixed', 'partial_data']
        scenario = random.choice(scenarios)
        
        print(f"  Task {task_num + 1}/{num_tasks}: {scenario} scenario")
        
        if scenario == 'success_first':
            # Success on first attempt
            data = {
                'status': 4,
                'bpm': random.randint(60, 100),
                'rpm': random.randint(12, 25),
                'signal_quality': round(random.uniform(0.8, 1.0), 2),
                'temperature': round(random.uniform(36.0, 37.5), 1),
                'device_id': 'VS-' + str(random.randint(1001, 9999))
            }
            timestamp = (task_base_time + timedelta(seconds=random.randint(5, 30))).isoformat()
            save_scan_data(db_path, task_id, data, 0, True, timestamp)
            
        elif scenario == 'success_retry':
            # Fail first few attempts, then succeed
            retry_attempts = random.randint(1, 3)
            
            # Failed attempts
            for i in range(retry_attempts):
                data = {
                    'status': random.choice([1, 2, 3]),
                    'bpm': random.randint(0, 50) if random.random() > 0.3 else 0,
                    'rpm': random.randint(0, 10) if random.random() > 0.3 else 0,
                    'signal_quality': round(random.uniform(0.1, 0.6), 2),
                    'error': random.choice(['Signal too weak', 'Motion detected', 'Sensor disconnected']),
                    'device_id': 'VS-' + str(random.randint(1001, 9999))
                }
                timestamp = (task_base_time + timedelta(minutes=i, seconds=random.randint(0, 59))).isoformat()
                save_scan_data(db_path, task_id, data, i, False, timestamp)
            
            # Success attempt
            data = {
                'status': 4,
                'bpm': random.randint(65, 95),
                'rpm': random.randint(15, 22),
                'signal_quality': round(random.uniform(0.7, 0.95), 2),
                'temperature': round(random.uniform(36.2, 37.2), 1),
                'device_id': 'VS-' + str(random.randint(1001, 9999))
            }
            timestamp = (task_base_time + timedelta(minutes=retry_attempts, seconds=random.randint(0, 59))).isoformat()
            save_scan_data(db_path, task_id, data, retry_attempts, True, timestamp)
            
        elif scenario == 'all_fail':
            # All 4 attempts fail
            for i in range(4):
                data = {
                    'status': random.choice([1, 2, 3]),
                    'bpm': random.randint(0, 40),
                    'rpm': random.randint(0, 8),
                    'signal_quality': round(random.uniform(0.1, 0.4), 2),
                    'error': random.choice(['No signal', 'Patient moved', 'Device malfunction', 'Low battery']),
                    'device_id': 'VS-' + str(random.randint(1001, 9999))
                }
                timestamp = (task_base_time + timedelta(minutes=i, seconds=random.randint(0, 59))).isoformat()
                save_scan_data(db_path, task_id, data, i, False, timestamp)
                
        elif scenario == 'mixed':
            # Mix of valid and invalid readings within same task
            attempts = random.randint(2, 4)
            for i in range(attempts):
                # Higher chance of success in later attempts
                success_probability = 0.3 + (i * 0.2)
                is_valid = random.random() < success_probability and i > 0
                
                if is_valid:
                    data = {
                        'status': 4,
                        'bpm': random.randint(70, 110),
                        'rpm': random.randint(14, 28),
                        'signal_quality': round(random.uniform(0.6, 0.9), 2),
                        'temperature': round(random.uniform(36.0, 37.8), 1),
                        'device_id': 'VS-' + str(random.randint(1001, 9999))
                    }
                else:
                    data = {
                        'status': random.choice([1, 2, 3]),
                        'bpm': random.randint(30, 60),
                        'rpm': random.randint(8, 15),
                        'signal_quality': round(random.uniform(0.2, 0.5), 2),
                        'error': random.choice(['Unstable signal', 'Calibration needed']),
                        'device_id': 'VS-' + str(random.randint(1001, 9999))
                    }
                
                timestamp = (task_base_time + timedelta(minutes=i, seconds=random.randint(0, 59))).isoformat()
                save_scan_data(db_path, task_id, data, i, is_valid, timestamp)
                
        elif scenario == 'partial_data':
            # Some readings missing data fields
            for i in range(random.randint(2, 3)):
                data = {
                    'status': random.choice([2, 3, 4]),
                    'device_id': 'VS-' + str(random.randint(1001, 9999))
                }
                
                # Randomly include/exclude fields
                if random.random() > 0.3:
                    data['bpm'] = random.randint(50, 120)
                if random.random() > 0.3:
                    data['rpm'] = random.randint(10, 30)
                if random.random() > 0.5:
                    data['signal_quality'] = round(random.uniform(0.3, 0.8), 2)
                
                is_valid = (data.get('status') == 4 and 
                          data.get('bpm', 0) > 0 and 
                          data.get('rpm', 0) > 0)
                
                timestamp = (task_base_time + timedelta(minutes=i, seconds=random.randint(0, 59))).isoformat()
                save_scan_data(db_path, task_id, data, i, is_valid, timestamp)

def print_database_stats(db_path):
    """Print statistics about the generated data."""
    conn = connect_db(db_path)
    cursor = conn.cursor()
    
    # Total records
    cursor.execute("SELECT COUNT(*) FROM sensor_scan_data")
    total_records = cursor.fetchone()[0]
    
    # Valid records
    cursor.execute("SELECT COUNT(*) FROM sensor_scan_data WHERE is_valid = 1")
    valid_records = cursor.fetchone()[0]
    
    # Unique tasks
    cursor.execute("SELECT COUNT(DISTINCT task_id) FROM sensor_scan_data")
    unique_tasks = cursor.fetchone()[0]
    
    # Average BPM for valid records
    cursor.execute("SELECT AVG(bpm) FROM sensor_scan_data WHERE is_valid = 1 AND bpm > 0")
    avg_bpm_result = cursor.fetchone()[0]
    avg_bpm = round(avg_bpm_result, 1) if avg_bpm_result else 0
    
    conn.close()
    
    print("\n" + "="*50)
    print("DATABASE STATISTICS")
    print("="*50)
    print(f"Total records: {total_records}")
    print(f"Valid records: {valid_records}")
    print(f"Success rate: {round((valid_records/total_records)*100, 1)}%" if total_records > 0 else "Success rate: 0%")
    print(f"Unique tasks: {unique_tasks}")
    print(f"Average BPM (valid): {avg_bpm}")
    print("="*50)

def main():
    """Main function to generate fake data."""
    print("Fake Vital Sensor Data Generator")
    print("="*50)
    
    # Get database path
    db_path = get_db_path()
    print(f"Database path: {db_path}")
    
    # Initialize database
    print("Initializing database...")
    init_database(db_path)
    
    # Ask user how many tasks to generate
    try:
        num_tasks = int(input("Enter number of scan tasks to generate (default: 15): ") or 15)
    except ValueError:
        num_tasks = 15
    
    # Generate fake data
    generate_fake_scan_tasks(db_path, num_tasks)
    
    # Print statistics
    print_database_stats(db_path)
    
    print(f"\nFake data generation complete!")
    print(f"You can now view the data at: http://localhost:8000/ui/sensor-data.html")
    print(f"Database file: {db_path}")

if __name__ == "__main__":
    main()