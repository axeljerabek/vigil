#!/usr/bin/env python3
import os
import sys
import psutil
import re
from datetime import datetime

MODEL_EXTENSIONS = ('.pt', '.onnx', '.engine', '.pth', '.safetensors', '.bin', '.tflite')

def get_process_vram():
    """Gibt ein Dictionary von PID -> VRAM zurück."""
    active_gpus = {}
    try:
        import subprocess
        cmd = "nvidia-smi --query-compute-apps=pid,used_memory --format=csv,noheader,nounits"
        out = subprocess.check_output(cmd, shell=True, text=True)
        for line in out.strip().split("\n"):
            if line:
                parts = line.split(",")
                if len(parts) >= 2:
                    pid = int(parts[0].strip())
                    vram = int(parts[1].strip())
                    active_gpus[pid] = vram
    except Exception:
        pass
    return active_gpus

def find_model_in_cmdlines(target_procs, cwd):
    """Durchsucht alle Cmdlines der Prozesse gezielt nach Modellnamen."""
    # Sammle alle Modell-Dateien im Workspace als bekannte Kandidaten
    known_models = {}
    if cwd and os.path.exists(cwd):
        for root, dirs, files in os.walk(cwd):
            dirs[:] = [d for d in dirs if d not in ('.venv', 'venv', '.git', '__pycache__', 'node_modules')]
            for file in files:
                if file.lower().endswith(MODEL_EXTENSIONS):
                    full_p = os.path.join(root, file)
                    known_models[file.lower()] = full_p

    # Prüfe Cmdlines aller Prozesse nach Vorkommen eines Modellnamens
    for proc in target_procs:
        try:
            cmdline_list = proc.info.get('cmdline') or []
            full_cmd = " ".join(cmdline_list)
            
            # Suche nach direktem Dateinamen in der Cmdline
            for model_name, model_path in known_models.items():
                if model_name in full_cmd.lower():
                    return model_path
                    
            # Fallback: Regex-Suche nach Dateiendungen in der Cmdline
            for ext in MODEL_EXTENSIONS:
                matches = re.findall(r'([\w\-\./]+' + re.escape(ext) + r')', full_cmd, re.IGNORECASE)
                for m in matches:
                    full_p = m if os.path.isabs(m) else os.path.normpath(os.path.join(cwd, m))
                    if os.path.exists(full_p):
                        return full_p
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue

    return None

def main():
    print("==================================================")
    print(" 👁️  RECORDER PIPELINE - AI MODEL & VRAM INSPECTOR")
    print("==================================================")

    target_procs = []
    workdirs = set()
    gpu_map = get_process_vram()

    for proc in psutil.process_iter(['pid', 'ppid', 'cmdline', 'cwd']):
        try:
            cmd = " ".join(proc.info['cmdline'] or [])
            if "recorder_pipeline" in cmd or "IDguard_PRO" in cmd or "web_ui.py" in cmd:
                target_procs.append(proc)
                if proc.info['cwd']:
                    workdirs.add(proc.info['cwd'])
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue

    if not target_procs:
        print("❌ Keine aktiven Recorder-Pipeline-Prozesse gefunden.")
        print("==================================================")
        return

    cwd = list(workdirs)[0] if workdirs else os.getcwd()
    active_model = find_model_in_cmdlines(target_procs, cwd)

    for proc in sorted(target_procs, key=lambda x: x.info['pid']):
        pid = proc.info['pid']
        ppid = proc.info['ppid']
        cmd = " ".join(proc.info['cmdline'] or [])
        cmd_short = cmd[:65] + "..." if len(cmd) > 65 else cmd

        print(f"\n⚙️  Prozess PID: {pid} (PPID: {ppid})")
        print(f"  ├─ Cmdline : {cmd_short}")
        
        vram_val = gpu_map.get(pid)
        if vram_val:
            print(f"  ├─ GPU     : {vram_val} MiB VRAM")
        else:
            print("  ├─ GPU     : -")

    print("\n--------------------------------------------------")
    if active_model and os.path.exists(active_model):
        size_mb = os.path.getsize(active_model) / (1024 * 1024)
        mtime = datetime.fromtimestamp(os.path.getmtime(active_model)).strftime('%Y-%m-%d %H:%M:%S')
        print("✅ Aktiv geladenes KI-Modell in der Pipeline:")
        print(f"   • {os.path.basename(active_model)}")
        print(f"     ├─ Pfad      : {active_model}")
        print(f"     ├─ Größe     : {size_mb:.2f} MB")
        print(f"     └─ Geändert  : {mtime}")
    else:
        print("⚠️  Es konnte kein Modell aus den Prozess-Cmdlines zugeordnet werden.")
    print("==================================================")

if __name__ == "__main__":
    main()
