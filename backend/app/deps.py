"""Dependencias FastAPI: acceso al estado compartido del nodo."""
from __future__ import annotations

from fastapi import Request

from .chaos import ChaosState
from .config import Settings
from .database import DatabaseCluster
from .peers import PeerClient


def get_settings(request: Request) -> Settings:
    return request.app.state.settings


def get_chaos(request: Request) -> ChaosState:
    return request.app.state.chaos


def get_cluster(request: Request) -> DatabaseCluster:
    return request.app.state.cluster


def get_peers(request: Request) -> PeerClient:
    return request.app.state.peers
