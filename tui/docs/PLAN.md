# Plan — TUI para DiLoCo (Textual)

## Objetivo
Dashboard en terminal para monitorear y controlar el coordinador DiLoCo y los nodos trabajadores.

## Stack
- **Textual** (Python) — TUI framework moderno.
- **httpx** — cliente HTTP async para consultar el coordinador.
- **rich** — tablas, colores, progress bars (incluido en Textual).

## Layout

```
┌─────────────────────────────────────────────────────────────┐
│ DiLoCo Dashboard — Coordinador: http://localhost:8095      │
├─────────────────────┬───────────────────────────────────────┤
│ Nodos (4)           │ Rondas                                │
│ ┌─────────────────┐ │ ┌───────────────────────────────────┐ │
│ │ node-0  active  │ │ │ Ronda 5  collecting  3/4 deltas  │ │
│ │ node-1  active  │ │ │ Ronda 4  completed   4/4         │ │
│ │ node-2  straggler│ │ │ Ronda 3  completed   4/4         │ │
│ │ node-3  offline │ │ └───────────────────────────────────┘ │
│ └─────────────────┘ │                                       │
├─────────────────────┴───────────────────────────────────────┤
│ Métricas                                                    │
│ Loss gap: +5.26%  |  CU: 87%  |  Goodput: 12.3 tok/s      │
│ Bytes/token: 0.54 MB  |  Stragglers: 1  |  Churn: 0       │
├─────────────────────────────────────────────────────────────┤
│ Logs (últimos 10)                                           │
│ [12:34:56] node-0: delta accepted (round 5)                 │
│ [12:34:57] node-2: heartbeat timeout → expulsado            │
│ [12:34:58] coordinator: round 5 complete                    │
├─────────────────────────────────────────────────────────────┤
│ [S]tart  [P]ause  [R]efresh  [Q]uit  [L]ogs                │
└─────────────────────────────────────────────────────────────┘
```

## Widgets

1. **Header**: título + URL del coordinador + estado de conexión.
2. **Nodos (DataTable)**: node_id, estado, CU, última ronda, heartbeat.
3. **Rondas (DataTable)**: ronda, estado, deltas recibidos, deadline.
4. **Métricas (Static)**: loss gap, CU, goodput, bytes/token, stragglers, churn.
5. **Logs (RichLog)**: eventos en tiempo real.
6. **Footer (Static)**: atajos de teclado.

## Funcionalidades

### Monitoreo
- Polling cada 2s al coordinador (`/status`, `/nodes`, `/rounds`).
- Actualización automática de tablas y métricas.
- Alertas cuando un nodo se expulsa o una ronda falla.

### Control
- `S` — Iniciar entrenamiento (envía POST /start al coordinador).
- `P` — Pausar (envía POST /pause).
- `R` — Refrescar manualmente.
- `L` — Toggle logs detallados.
- `Q` — Salir.

### Configuración
- URL del coordinador (argumento CLI o env var).
- Intervalo de polling (default 2s).
- Tema (dark/light).

## Fases

1. **Fase 1**: Dashboard básico (nodos + rondas + métricas).
2. **Fase 2**: Control (start/pause/refresh).
3. **Fase 3**: Logs en tiempo real + alertas.
4. **Fase 4**: Integración con gateway WebSocket (métricas push).

## Dependencias
```bash
pip install textual httpx rich
```

## Inicio rápido
```bash
cd diloco-tui
python -m diloco_tui --coordinator http://localhost:8095
```