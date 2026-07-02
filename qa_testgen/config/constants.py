import pytz
from pathlib import Path
from reportlab.lib import colors

BASE_DIR = Path(__file__).resolve().parents[1]
LOGO_PATH = BASE_DIR / "logo_refu_1.png"
SIMBOLO_PATH = BASE_DIR / "simbolo_refu_1.png"
TZ_BR = pytz.timezone('America/Sao_Paulo')

COR_LARANJA = colors.HexColor('#F15A24')
COR_CINZA_ESC = colors.HexColor('#3A3A3A')
COR_CINZA_MED = colors.HexColor('#6B6B6B')
COR_LARANJA_CLARO = colors.HexColor('#FAE5DC')
COR_AZUL_CLARO = colors.HexColor('#DCE8FA')
COR_CINZA_LIN = colors.HexColor('#F5F5F5')
COR_BRANCO = colors.white
