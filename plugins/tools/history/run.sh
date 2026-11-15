#!/bin/sh
# The engine's own `agentflow history`, for an agent. The engine says which flow and which run are asking.
exec agentflow history --item "$1"
