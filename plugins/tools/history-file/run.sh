#!/bin/sh
# The engine's own `agentflow history --file`, for an agent.
exec agentflow history --run "$1" --item "$2" --file "$3"
