# pow_etl — ETL adicional para el tablero PowerBI (FBD/Fratelli)

Complementa a `POW_GENERA_SCRIPT` / `UDMG2Kweb` (que sigue igual, sin tocar).
Cubre las piezas del modelo que ese flujo no genera:
- `pow_agentes` (DIM_VENDEDOR) — probado OK.
- `pow_compras_cab` / `pow_compras_det` (FACT_COMPRAS / Sell In) — trae TODAS
  las compras facturadas de Castro (no filtra por proveedor; ver nota en
  `ddl_pow_compras.sql`). Toma un rango de fechas (`--desde`/`--hasta`).
- `pow_stock` (FACT_STOCK) — foto del stock ACTUAL de G2K, guardada con la
  fecha `--hasta` (hoy por defecto). No es histórico: para tener serie
  temporal hay que correrlo periódicamente (todavía sin tarea programada).
- `legado` (7-oct-2026) — **migración de `UDMG2Kweb.pas`/`UFMG2Kweb.pas`**:
  llama al mismo `POW_GENERA_SCRIPT` sin tocarlo, con las 4 piezas
  GENERA_CLI/FAC/AGE/ART activas (`pow_clientes`, `pow_fac_cab`/`pow_fac_det`,
  `pow_cli_agentes`, `pow_articulos`), ventana rodante de `--legado-dias`
  días hacia atrás desde hoy (default 4, igual que `SYS_CONSTANTES.G2KWEB_EST_MESES`
  en producción). Reemplaza el timer interno de G2K (que exige tener
  `G2K1.exe` abierto) por la tarea programada de `pow_etl.py`. El SP de
  InterBase sigue igual; solo cambió quién lo llama y aplica el resultado a
  MySQL — antes Delphi (`TDMG2Kweb.EjecutarScript`), ahora Python. La
  tabla de staging (`TMP_POW_SCRIPTS`, sin columna `renglon`) se lee grupo
  por grupo (`WHERE grupo=?`), nunca con un solo `ORDER BY grupo` sobre toda
  la tabla — el motor no garantiza orden estable entre filas de un mismo
  grupo cuando son miles, y eso rompe la reconstrucción de las sentencias
  (confirmado con un `--dry-run` real antes de aplicar nada).
- `pow_articulos_ext` / `pow_clientes_ext` — enriquecimiento de DIM_PRODUCTO
  y DIM_CLIENTE. Reflejan campos NUEVOS y propios agregados en
  `ART_ARTICULOS`/`EMP_CLIENTES` (ver `ScL17_190_POW_CAMPOS_NUEVOS.sql`) —
  marca, litros_por_unidad, SKU, categoría, presentación, unidades_por_caja
  del lado artículos; canal_pow, subcanal, fecha_alta, fecha_baja,
  relevancia_interna, potencial, etiqueta_segmento, zona del lado clientes.
  Se cargan a mano en G2K (pantalla nueva, a futuro); IB es la fuente de
  verdad, estas tablas son un espejo (el ETL los pisa todos en cada
  corrida). Son tablas de EXTENSIÓN separadas de `pow_articulos`/
  `pow_clientes` a propósito: esas dos las sigue armando
  `POW_GENERA_SCRIPT`/`UDMG2Kweb` con `replace into`, que en MySQL borra y
  reinserta la fila — si estas columnas estuvieran ahí, cada corrida de
  `UDMG2Kweb` las volvería a vaciar.

## Cómo funciona

1. Un SP nuevo en InterBase (`POW_GENERA_AGENTES`, ver
   `Scripts_GDB\L17_Castro\ScL17_187_POW_AGENTES.sql`) arma el SQL a correr y
   lo deja en `TMP_POW_SCRIPTS_ETL` — una tabla de staging **separada** de
   `TMP_POW_SCRIPTS` (la que usa el flujo de ventas existente), para no
   interferir con nada que ya está en producción.
2. `pow_etl.py` ejecuta el SP, lee `TMP_POW_SCRIPTS_ETL` y aplica el SQL
   resultante contra MySQL. Usa `INSERT .. ON DUPLICATE KEY UPDATE`, no
   `REPLACE INTO`: correrlo de nuevo actualiza los datos que vienen de G2K
   (hoy, el nombre) pero no pisa columnas que se completen a mano en MySQL
   (zona, estado — ver `ddl_pow_agentes.sql`).

No requiere recompilar `G2K1.exe`.

## Instalación

1. Copiar esta carpeta completa (incluye `fdb\`, copiado de `../envio/fdb/`).
2. Python 3.12 de 32 bits (mismo requisito que `sync_envio` — `gds32.dll` es
   32-bit). Instalar `mysql-connector-python` (`pip install mysql-connector-python`).
3. Correr una vez contra MySQL: `ddl_pow_agentes.sql`, `ddl_pow_compras.sql`,
   `ddl_pow_stock.sql`, `ddl_pow_articulos_ext.sql` y `ddl_pow_clientes_ext.sql`.
4. Correr en InterBase, EN ESTE ORDEN (`Scripts_GDB\L17_Castro\`):
   `ScL17_187_POW_AGENTES.sql`, `ScL17_188_POW_COMPRAS.sql`,
   `ScL17_189_POW_STOCK.sql`, `ScL17_190_POW_CAMPOS_NUEVOS.sql` (agrega los
   campos nuevos a ART_ARTICULOS/EMP_CLIENTES),
   `ScL17_191_POW_ENRIQUECE_ARTICULOS.sql` y
   `ScL17_192_POW_ENRIQUECE_CLIENTES.sql`. `ScL17_190` tiene que correr
   ANTES que 191/192 (esos SP leen los campos que 190 agrega).
5. Copiar `pow_etl.ini.example` a `pow_etl.ini` y completar (mismo formato
   que `sync_envio.ini`). No commitear `pow_etl.ini`.
6. Probar: `py -3.12-32 pow_etl.py agentes --dry-run`
7. Correr en serio: `py -3.12-32 pow_etl.py agentes`
8. Compras (con rango de fechas; por defecto trae desde 2020-01-01 hasta hoy):
   `py -3.12-32 pow_etl.py compras --dry-run`
   `py -3.12-32 pow_etl.py compras --desde 2025-01-01 --hasta 2026-09-14`
9. Stock (foto de hoy): `py -3.12-32 pow_etl.py stock --dry-run`
10. Enriquecimiento de artículos y clientes:
    `py -3.12-32 pow_etl.py articulos clientes --dry-run`
11. Todas juntas: `py -3.12-32 pow_etl.py all`

## Tarea programada (producción)

Igual que `sync_cobros` (21:00) y `sync_envio` (22:00), `pow_etl.py all` se
corre una vez por día contra producción con una tarea de Windows a las 23:00:

1. Copiar `pow_etl.prod.ini.example` a `pow_etl.prod.ini` y completar (host
   de InterBase/MySQL reales de producción — NO el `pow_etl.ini` de test).
2. Correr `agregar_tarea.bat` como Administrador — crea la tarea
   `CastroPowETL`, diaria a las 23:00, vía `run_pow_etl.bat`.
3. Probar a mano una vez: `run_pow_etl.bat` (o `py -3.12-32 pow_etl.py all -c pow_etl.prod.ini`).

`compras` corre siempre con el rango completo por defecto (2020-01-01 a
hoy) — es `insert .. on duplicate key update`, así que repetir el rango no
duplica nada, solo actualiza. A medida que crezca el historial, si se
vuelve lento, cambiar `run_pow_etl.bat` para pasar un `--desde` más acotado
(por ejemplo los últimos 60 días).

## Agregar una pieza nueva (compras, stock, ...)

1. SP en IB que arme el script en `TMP_POW_SCRIPTS_ETL` (mismo patrón que
   `POW_GENERA_AGENTES`: `grupo`/`renglon` para el orden, fragmentos de
   hasta 1024 caracteres, `insert .. on duplicate key update` como
   terminador de cada grupo en vez de `replace into`).
2. Tabla nueva en MySQL (`ddl_pow_XXX.sql`).
3. Agregar la entrada en el diccionario `PIEZAS` de `pow_etl.py`.
