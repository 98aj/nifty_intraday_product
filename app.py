"""Nifty weekly options and a Bank Nifty late-session desk.

The Nifty live desk is the first page. Bank Nifty and Backtest follow.
Keep each page function in ``modules/ui``.

`streamlit run app.py`
"""

from __future__ import annotations

import sys
from pathlib import Path

import streamlit as st

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from modules.ui.styles import inject_css

st.set_page_config(
    page_title="Options desk",
    layout="wide",
    page_icon="▣",
    initial_sidebar_state="expanded",
)
inject_css()

navigation = st.navigation(
    [
        st.Page("modules/ui/desk_page.py", title="Live desk", default=True),
        st.Page("modules/ui/nifty2_desk_page.py", title="Nifty 2"),
        st.Page("modules/ui/banknifty_desk_page.py", title="Bank Nifty"),
        st.Page("modules/ui/backtest_page.py", title="Backtest"),
    ]
)
navigation.run()
