"""Write frontend/src/settingsLimits.json from the Settings model's numeric bounds.

Run after changing a bound in backend/models.py; a test fails while the two differ.
"""
import json
import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parent.parent))
from backend.models import Settings

OUTPUT=Path(__file__).resolve().parent.parent/'frontend'/'src'/'settingsLimits.json'

def limits():
    # Required numeric settings only; optional prices have no upper bound or form check.
    result={}
    for name,field in Settings.model_fields.items():
        if field.annotation not in (int,float): continue
        bounds={type(m).__name__:m for m in field.metadata}
        result[name]={'min':bounds['Ge'].ge,'max':bounds['Le'].le if 'Le' in bounds else None,'integer':field.annotation is int}
    return result

def render(): return json.dumps(limits(),ensure_ascii=False,indent=2)+'\n'

if __name__=='__main__':
    OUTPUT.write_text(render())
    print(OUTPUT)
