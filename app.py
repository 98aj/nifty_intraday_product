"""Nifty weekly options live desk.

The Nifty live desk is the only page. Keep the page function in ``modules/ui``.

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
    ]
)
navigation.run()
