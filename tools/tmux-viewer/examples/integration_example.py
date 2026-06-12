"""Quick-start integration example for the tmux viewer backend."""

import asyncio
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from tmux_viewer.backend.api import router as tmux_router, init_manager
from tmux_viewer.backend.tmux_manager import TmuxSessionManager

app = FastAPI(title="Tmux Viewer API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

manager = TmuxSessionManager(session_prefix="myapp")
init_manager(manager)

app.include_router(tmux_router, prefix="/api")


@app.on_event("startup")
async def startup():
    session = manager.create_session(
        session_name="demo-session",
        working_directory="/tmp",
        env_vars={"MY_VAR": "hello"},
    )
    pane = session.attached_window.attached_pane
    pane.send_keys("echo 'Tmux Viewer is live!'", enter=True)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8080)
