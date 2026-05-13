import json
import time
import random
from pathlib import Path

SIGNAL_PATH = Path("data/quant_bitcoin_signals.json")

def simulate():
    print(f"🚀 Starting Mirofish Signal Simulator...")
    print(f"📝 Writing to {SIGNAL_PATH}")
    
    SIGNAL_PATH.parent.mkdir(parents=True, exist_ok=True)
    
    while True:
        # Simulate a BULL convergence
        # 100+ nodes, 180+ edges
        bull_nodes = random.randint(60, 80)
        bear_nodes = random.randint(40, 50)
        total_nodes = bull_nodes + bear_nodes
        edges = random.randint(180, 250)
        
        # Validators converge to BULL
        data = {
            "tradingview": "BULL",
            "cryptoquant": "BULL",
            "graph_bias": "BULL",
            "bull_nodes": bull_nodes,
            "bear_nodes": bear_nodes,
            "edges": edges,
            "ts": time.time()
        }
        
        with open(SIGNAL_PATH, "w") as f:
            json.dump(data, f)
            
        print(f"📡 [SIMULATOR] Sent BULL Signal: Nodes={total_nodes} ({bull_nodes}/{bear_nodes}) Edges={edges}")
        
        # Wait 10 seconds before next update
        time.sleep(10)

if __name__ == "__main__":
    try:
        simulate()
    except KeyboardInterrupt:
        print("\nStopping simulator.")
