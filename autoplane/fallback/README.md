# Crewplane local con fallback de proveedores

Este es un repositorio de prueba. Crewplane orquesta cuatro etapas persistidas y `agent-fallback` rota entre los CLIs oficiales sin usar proxies, tokens extraídos ni Ollama.

## Arquitectura

`thinker` usa Claude → Codex → Antigravity; `coder`, Codex → Claude → Antigravity; y `reviewer`, Antigravity → Claude → Codex. El dispatcher lee el prompt de stdin, escribe sólo la respuesta elegida a stdout y registra en stderr/metadatos el proveedor y la clasificación. Las reglas compartidas canónicas están en `.agent/AGENT_RULES.md`; `CLAUDE.md` y `AGENTS.md` sólo la referencian.

## Instalación

No instala paquetes. Requiere `python3`, `crewplane`, `claude`, `codex` y `agy` ya autenticados mediante sus mecanismos oficiales. Desde este directorio:

```bash
chmod +x bin/agent-fallback
python3 -m unittest discover -s tests -v
```

Para exponer el comando sólo en la shell actual:

```bash
export PATH="$PWD/bin:$PATH"
```

## Uso

Prueba el dispatcher sin escribir en el proyecto:

```bash
printf '%s' 'Resume el estado del repositorio.' | bin/agent-fallback --role thinker
```

Valida y ejecuta el flujo:

```bash
crewplane validate .crewplane/workflows/multi-agent-flow.task.md
crewplane run --tasks .crewplane/workflows/multi-agent-flow.task.md --no-live
```

Para revisar una corrida terminada, sin tmux y sin que Crewplane siga vivo:

```bash
bin/crewplane-view                      # corridas, de la mas reciente a la mas antigua
bin/crewplane-view latest               # linea de tiempo por nodo y cadena de fallback
bin/crewplane-view latest implementation.coder   # progreso completo y salida de un nodo
```

El panel tmux sólo existe mientras corre Crewplane: sus paneles hacen `cat` de archivos que el propio proceso reescribe, así que al terminar la corrida el panel deja de repintarse aunque la sesión siga abierta. El visor lee los artefactos persistidos y no tiene esa limitación.

Cada fase recibe los outputs anteriores con `needs` y `{{nodo.output}}`. Crewplane guarda las ejecuciones bajo `.crewplane/execution-stages/` y `.crewplane/execution-results/`; se ignoran en Git porque pueden contener contenido de prompts o respuestas.

## Operación segura

El perfil de lectura no autoriza escritura: Claude usa `--permission-mode plan`, Codex fuerza `--sandbox read-only` y Antigravity usa `--mode=plan --sandbox`. El perfil de escritura, usado sólo por el rol `coder` sobre worktrees aislados, usa Antigravity `--mode=accept-edits --add-dir {cwd}`, Codex `--sandbox workspace-write` y Claude `--permission-mode acceptEdits`. Ninguno lleva flags de bypass: Antigravity concede permisos por `trustedWorkspaces` de su propia configuración, y `--add-dir` es obligatorio porque sin él sus herramientas de archivo resuelven contra su scratch y reporta éxito sin tocar el worktree. No añadas flags de bypass ni modos de aceptación automática sin una revisión explícita.

Los patrones editables de cuota, indisponibilidad y autenticación están en `config/agent-fallback.json`. Para desactivar temporalmente un proveedor, cambia su `enabled` a `false`. Para cambiar prioridad, edita `role_order` en ese JSON. Para elegir modelo por tipo de tarea, edita `role_models`: asigna un modelo a cada par rol/proveedor y el dispatcher lo inserta donde el comando declara `{model}`. Sin entrada para ese par, manda el modelo por omisión del CLI. El tope absoluto por intento es `timeout_seconds` (1800s en lectura, 5400s en escritura), ajustable también con `--timeout-seconds`. Por encima de él manda `idle_timeout_seconds`: un proveedor que no emite nada durante ese tiempo se considera colgado y se termina, mientras que uno que sigue emitiendo eventos se deja trabajar. Un reloj absoluto no distingue un agente lento de uno bloqueado; la ausencia de salida sí. Cada proveedor se intenta una sola vez por invocación.

Un proveedor clasificado `quota` o `auth` queda en enfriamiento durante `cooldown_seconds` (1800 por omisión) y se omite en las invocaciones siguientes, que lo anuncian en stderr. El estado vive en `cooldown_path` y se reemplaza de forma atómica, porque varios agentes pueden invocar el dispatcher a la vez. Un archivo ausente o corrupto significa que no hay enfriamientos: el registro de estado nunca impide intentar un proveedor. Si todos los del rol estuvieran enfriándose, se ignoran los enfriamientos y se intenta igual; chocar contra un muro es preferible a no hacer nada.

Cada CLI usa su salida estructurada oficial en modo incremental: `stream-json` para Claude y Antigravity, `--json` para Codex. Los tres emiten NDJSON, de modo que el progreso aparece en los artefactos de Crewplane mientras el agente trabaja en lugar de llegar completo al final. El dispatcher extrae únicamente la respuesta final; los errores se clasifican desde el protocolo y no desde prompts repetidos, contenidos leídos o resultados de herramientas. A stderr sólo se emiten etiquetas condensadas del evento (`assistant:tool:Bash`, `result:success`), nunca la carga útil: reemitirla duplicaría cada respuesta dentro del log y falsearía la estimación de texto visible. Un resultado técnico negativo sigue siendo válido. Los diagnósticos se emiten al terminar cada intento, sin almacenar la salida cruda. Al vencer el timeout se termina el grupo de procesos local del proveedor.

El workflow actual es de propuestas: incluye los seis documentos de memoria en cada etapa y prohíbe ejecutar herramientas u otros agentes. Esto evita que Antigravity intente `RunCommand` para obtener contexto y termine vacío porque el modo headless deniega el permiso. No implementa los planes de Tencent ni ejecuta pruebas del producto.

## Actualización y desinstalación

Actualiza cada CLI sólo con su mecanismo oficial y vuelve a ejecutar `--version`, `--help` y las pruebas. No hay configuración global creada por este repositorio. Para desinstalar esta prueba, primero conserva o elimina manualmente el directorio del repositorio; no se modificaron proyectos reales, autenticaciones ni configuraciones globales.

Para ejecutar sin consumo, utiliza el archivo separado, sin editar la configuración real:

```bash
crewplane run --config .crewplane/config-mock.yml --tasks .crewplane/workflows/multi-agent-flow.task.md --no-live
```

Los artefactos de corridas previas se conservan. Este repositorio aún no tiene un commit inicial: no hay una versión previa recuperable mediante Git. No uses `cleanup` si necesitas conservar los handoffs.

## Limitaciones conocidas

Verificación 2026-09-14: 14 pruebas unitarias aprobadas; mock `20260914-165237` y CLI real `20260914-165154` completos, cuatro etapas exitosas y cero errores. La corrida real duró 60 segundos usando Claude → Codex → Antigravity → Claude. Resumen en `.crewplane/execution-stages/flujo-local-con-fallback--99a144913039-20260914-165154/logs/summary.md`.

La continuidad depende de que al menos un proveedor produzca una respuesta válida dentro del timeout. Los antiguos registros `codex=quota` no prueban por sí solos agotamiento de cuota: el clasificador anterior inspeccionaba también el prompt repetido. Verificación 2026-09-14: el mensaje real de Codex agotado es «You've hit your usage limit. … purchase more credits …», que ningún patrón cubría; se clasificaba `provider_error`. Los patrones `hit your usage limit` y `purchase more credits` se añadieron con ese texto observado y una prueba que lo fija. Antigravity puede devolver `SUCCESS` con respuesta vacía y `denied_actions`; ahora se identifica como `permission_denied` y se intenta el siguiente proveedor sin conceder permisos nuevos.

El prompt de Antigravity se pasa como argumento, por lo que puede ser visible para herramientas locales de inspección de procesos; no envíes secretos. Los procesos oficiales conservan sus propios mecanismos de autenticación y almacenamiento. Crewplane persiste prompts y respuestas en sus artefactos de handoff; los logs propios del dispatcher sólo guardan metadatos.
