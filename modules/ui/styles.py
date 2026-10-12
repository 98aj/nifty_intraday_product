"""High-contrast, scannable desk CSS injected once per Streamlit run."""

DASHBOARD_CSS = """
<style>
    .stApp { background: #0b0f14; color: #f4f6f8; }
    header[data-testid="stHeader"] { background: #0b0f14; }
    section[data-testid="stSidebar"] { background: #0e141c; border-right: 1px solid #243041; }
    .block-container { padding-top: 1.1rem; max-width: 1440px; }

    .desk-title {
        font-size: 1.55rem; font-weight: 750; letter-spacing: 0.03em;
        margin: 0 0 0.15rem 0; color: #f4f6f8;
    }
    .desk-sub { color: #9aa8b7; font-size: 0.92rem; margin-bottom: 0.55rem; }

    .status-pill {
        display: inline-block; padding: 0.22rem 0.7rem; border-radius: 999px;
        font-size: 0.78rem; font-weight: 700; letter-spacing: 0.06em;
        margin: 0.12rem 0.18rem 0.12rem 0;
    }
    .ok { background: #113226; color: #3dd68c; border: 1px solid #1f7a4d; }
    .bad { background: #3a1218; color: #ff6b7a; border: 1px solid #8b2430; }
    .warn { background: #3a2a10; color: #ffc857; border: 1px solid #8a6a1f; }

    .feature-row { display: flex; flex-wrap: wrap; gap: 0.4rem; margin: 0.15rem 0 0.85rem 0; }
    .feature-chip {
        background: #141b23; border: 1px solid #2a3646; border-radius: 999px;
        padding: 0.28rem 0.75rem; font-size: 0.78rem; color: #c5d0db; font-weight: 650;
    }

    .mode-banner {
        background: #12261c; border: 1px solid #1f7a4d; border-radius: 12px;
        padding: 0.85rem 1rem; margin-bottom: 0.85rem; color: #d7eee3;
    }
    .mode-banner.demo {
        background: #2a2414; border-color: #8a6a1f; color: #efe4c4;
    }
    .mode-banner strong { color: #ffffff; }

    .today-card {
        background: #141b23; border: 1px solid #2a3646; border-radius: 12px;
        padding: 0.8rem 0.85rem; margin: 0.2rem 0 0.75rem 0;
    }
    .today-card .kicker {
        color: #8b9aab; font-size: 0.7rem; font-weight: 700;
        letter-spacing: 0.08em; text-transform: uppercase;
    }
    .today-card .action { font-size: 1.35rem; font-weight: 780; margin: 0.2rem 0; }
    .today-card .hint { color: #9aa8b7; font-size: 0.82rem; line-height: 1.4; }

    .metric-card {
        background: #141b23; border: 1px solid #2a3646; border-radius: 12px;
        padding: 0.95rem 1rem 0.85rem 1rem; min-height: 96px;
    }
    .metric-label {
        color: #8b9aab; font-size: 0.75rem; font-weight: 700;
        letter-spacing: 0.08em; text-transform: uppercase;
    }
    .metric-value { font-size: 1.55rem; font-weight: 760; line-height: 1.2; margin-top: 0.25rem; }
    .metric-value.buy { color: #3dd68c; }
    .metric-value.sell { color: #ff6b7a; }
    .metric-value.scan { color: #ffc857; }
    .metric-hint { color: #9aa8b7; font-size: 0.78rem; margin-top: 0.28rem; line-height: 1.35; }

    .hero {
        border-radius: 14px; padding: 1rem 1.15rem 0.95rem;
        margin: 0.15rem 0 0.85rem 0; border: 1px solid #2a3646; background: #141b23;
    }
    .hero .kicker {
        color: #8b9aab; font-size: 0.72rem; font-weight: 700;
        letter-spacing: 0.08em; text-transform: uppercase;
    }
    .hero .action { font-size: 1.55rem; font-weight: 780; margin: 0.15rem 0 0.3rem 0; color: #f4f6f8; }
    .hero p { margin: 0; color: #c5d0db; font-size: 0.95rem; line-height: 1.45; }
    .hero.wait { border-color: #8a6a1f; }
    .hero.go { border-color: #1f7a4d; background: #12261c; }
    .hero.stop { border-color: #8b2430; background: #2a1418; }
    .hero.hold { border-color: #3d7dff; }

    .ticket {
        background: #12261c; border: 1px solid #1f7a4d; border-radius: 12px;
        padding: 0.95rem 1.05rem; margin: 0.35rem 0 0.7rem 0;
    }
    .ticket h3 { margin: 0 0 0.35rem 0; color: #f4f6f8; font-size: 1.05rem; }
    .ticket p { margin: 0.18rem 0; color: #d7eee3; font-size: 0.92rem; line-height: 1.45; }

    .ticket-grid {
        display: grid; grid-template-columns: repeat(6, minmax(0, 1fr));
        gap: 0.55rem; margin: 0.35rem 0 0.7rem 0;
    }
    .reason-box {
        background: #141b23; border-left: 4px solid #3d7dff; border-radius: 8px;
        padding: 0.75rem 1rem; color: #d7dee6; margin: 0.2rem 0 0.75rem 0;
        font-size: 0.92rem; line-height: 1.45;
    }
    .guide-card {
        background: #141b23; border: 1px solid #2a3646; border-radius: 12px;
        padding: 0.95rem 1rem; min-height: 148px; margin-bottom: 0.65rem;
    }
    .guide-card h4 { margin: 0 0 0.4rem 0; color: #f4f6f8; font-size: 0.98rem; }
    .guide-card p { margin: 0; color: #9aa8b7; font-size: 0.86rem; line-height: 1.5; }

    .brief-box {
        background: #141b23; border-left: 4px solid #3d7dff; border-radius: 8px;
        padding: 0.85rem 1rem; color: #d7dee6; margin-bottom: 0.8rem;
    }

    div.stButton > button {
        width: 100%; min-height: 58px; font-weight: 800; letter-spacing: 0.03em;
        border-radius: 10px; font-size: 0.95rem;
    }
    div[class*="st-key-pause_button"] button {
        min-height: 52px !important;
        background: #3a2a10 !important;
        color: #ffe7a3 !important;
        border: 2px solid #ffc857 !important;
    }
    div[class*="st-key-resume_button"] button {
        min-height: 52px !important;
        background: #3a1218 !important;
        color: #ffd0d4 !important;
        border: 2px solid #ff6b7a !important;
    }
    div[data-testid="stSidebar"] div.stButton > button { min-height: 42px; }

    .sqoff-wrap div.stButton > button {
        background: #c1121f !important; color: #fff !important;
        border: 2px solid #ff4d5a !important;
        box-shadow: 0 0 0 3px rgba(193, 18, 31, 0.25);
    }
    .sqoff-wrap div.stButton > button:hover {
        background: #e01e2b !important; border-color: #ff808a !important;
    }
</style>
"""


def inject_css() -> None:
    import streamlit as st

    st.markdown(DASHBOARD_CSS, unsafe_allow_html=True)
