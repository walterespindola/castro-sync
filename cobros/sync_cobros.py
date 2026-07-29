#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
sync_cobros.py
Sincronizacion directa MySQL -> InterBase de cobros moviles.
Reemplaza el paso intermedio BDE/Paradox.

Secuencia equivalente al proceso manual en Delphi:
  1. Por cada cobro pendiente: EXECUTE PROCEDURE E_CARTERA_NUEVO_COBRO  + commit IB
  2. Desmarcar deudas enviadas: EXECUTE PROCEDURE MOV_MARCA_DEUDAS_EN_MOVIL (no critico)
  3. Imputar cobros:            EXECUTE PROCEDURE E_CARTERA_IMPUTA_COBROS   + commit IB

Requiere:
    pip install mysql-connector-python kinterbasdb
    (o pyodbc con driver ODBC de InterBase instalado)

Uso:
    python sync_cobros.py [ruta_config.ini]
"""

import sys
import os
import socket
import logging
import configparser
from datetime import datetime, date
from decimal import Decimal

# Si hay un subdirectorio 'fdb/' junto al script, usarlo (version parcheada para IB 5.6)
_here = os.path.dirname(os.path.abspath(__file__))
if os.path.isdir(os.path.join(_here, 'fdb')):
    sys.path.insert(0, _here)

import mysql.connector

# fdb usa gds32.dll directamente via ctypes — funciona con InterBase 5.6
import fdb
DRIVER = 'fdb'

# ─── Constantes ───────────────────────────────────────────────────────────────

DEFAULT_CONFIG = 'sync_cobros.ini'

FORMA_PAGO_CONTADO = 'COS'   # cFormaDePago en UEntorno.pas de Castro

FORMATOS_FECHA = (
    '%Y-%m-%d %H:%M:%S',
    '%Y-%m-%d',
    '%d/%m/%Y %H:%M:%S',
    '%d/%m/%Y',
    '%d-%m-%Y',
)


# ─── Helpers IB literal SQL ───────────────────────────────────────────────────
# IB 5.6 DSQL no acepta '?' en EXECUTE PROCEDURE; hay que embeber valores literales.

_IB_MONTHS = ('JAN','FEB','MAR','APR','MAY','JUN',
              'JUL','AUG','SEP','OCT','NOV','DEC')

def ib_lit(v):
    """Convierte un valor Python a literal SQL para IB 5.6 dialect 1."""
    if v is None:
        return 'NULL'
    if isinstance(v, bool):
        return '1' if v else '0'
    if isinstance(v, int):
        return str(v)
    if isinstance(v, (float, Decimal)):
        # str(Decimal('-150.00')) → '-150.00' sin comillas (literal numerico)
        return str(v)
    if isinstance(v, date):
        # IB 5.6 entiende DD-MON-YYYY independientemente del locale del servidor
        return "'{:02d}-{}-{}'".format(v.day, _IB_MONTHS[v.month - 1], v.year)
    return "'" + str(v).replace("'", "''") + "'"


def ib_exec(proc_name, params):
    """Devuelve 'EXECUTE PROCEDURE nombre(val1, val2, ...)' con valores literales."""
    return 'EXECUTE PROCEDURE {}({})'.format(
        proc_name, ', '.join(ib_lit(p) for p in params)
    )

SQL_COBROS_PENDIENTES = """
    SELECT c.id, c.imei, c.cliente, c.tipo, c.serie, c.rig,
           c.fecha, c.pago, c.ric,
           i.agente AS agente_cobro
    FROM cli_cobros c
    JOIN imeis i ON i.imei = c.imei
    WHERE c.sync IN (0, 1)
      AND i.agente < 5000
    ORDER BY c.id
"""

# Verifica si el cobro ya fue incorporado a emp_cartera
SQL_YA_EXISTE = (
    "SELECT COUNT(*) FROM emp_cartera "
    "WHERE empresa=? AND ejercicio=? AND canal=? "
    "AND agente=? AND cod_cli_pro=? "
    "AND registro_palm IS NOT NULL AND registro_palm=? "
    "AND fecha_registro=?"
)

SQL_MARCAR_SYNC = "UPDATE cli_cobros SET sync=2 WHERE id IN ({ids})"


# ─── Helpers ──────────────────────────────────────────────────────────────────

def parsear_fecha(s):
    if not s:
        return date.today()
    s = str(s).strip()
    for fmt in FORMATOS_FECHA:
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    raise ValueError(f"No se pudo parsear la fecha: {s!r}")


# ─── Conexion InterBase ───────────────────────────────────────────────────────

def obtener_entrada(con_ib, empresa):
    """Registra una nueva sesion en IB y devuelve el numero de entrada generado.
    Replica la logica de LOG_ENTRADAS_ALTA: busca/crea ubicacion en
    sys_ubicaciones, genera ID via genera conta_entradas e inserta en sys_entradas.
    Equivalente al login del usuario en Castro."""
    maquina = socket.gethostname()[:31]
    login   = 'sync_cobros'

    cur = con_ib.cursor()

    # Buscar o crear ubicacion para este equipo/login
    cur.execute(
        "SELECT ubicacion FROM sys_ubicaciones WHERE maquina=? AND login=?",
        (maquina, login)
    )
    row = cur.fetchone()
    if row:
        ubicacion = row[0]
    else:
        cur.execute("SELECT gen_id(conta_ubicaciones, 1) FROM RDB$DATABASE")
        ubicacion = cur.fetchone()[0]
        cur.execute(
            "INSERT INTO sys_ubicaciones(ubicacion, maquina, login, sistema)"
            " VALUES(?, ?, ?, ?)",
            (ubicacion, maquina, login, 'sync_cobros')
        )

    # Generar nuevo numero de entrada
    cur.execute("SELECT gen_id(conta_entradas, 1) FROM RDB$DATABASE")
    entrada = cur.fetchone()[0]

    # Registrar la entrada (usuario=1, empresa pasada por parametro)
    cur.execute(
        "INSERT INTO sys_entradas(entrada, usuario, empresa, ubicacion)"
        " VALUES(?, ?, ?, ?)",
        (entrada, 1, empresa, ubicacion)
    )

    cur.close()
    con_ib.commit()
    return entrada


def conectar_ib(cfg):
    host     = cfg.get('interbase', 'host')
    database = cfg.get('interbase', 'database')
    user     = cfg.get('interbase', 'user')
    password = cfg.get('interbase', 'password')
    gds32    = cfg.get('interbase', 'gds32',
                       fallback=r'C:\Desarrollo\Castro\Compilacion\gds32.dll')
    con = fdb.connect(
        host             = host,
        database         = database,
        user             = user,
        password         = password,
        sql_dialect      = 1,       # InterBase 5.6 usa dialecto 1
        fb_library_name  = gds32,
    )
    # Test rapido para verificar que el DSQL funciona
    cur = con.cursor()
    cur.execute('SELECT COUNT(*) FROM EMP_CARTERA')
    cur.fetchone()
    cur.close()
    return con


# ─── Logica de negocio ────────────────────────────────────────────────────────

def ya_existe_cobro(cur_ib, empresa, ejercicio, canal,
                    agente, cliente, registro_palm, fecha):
    cur_ib.execute(SQL_YA_EXISTE,
                   (empresa, ejercicio, canal, agente, cliente, registro_palm, fecha))
    row = cur_ib.fetchone()
    return bool(row and row[0] > 0)


def incorporar_cobro(con_ib, empresa, ejercicio, canal, entrada, cobro):
    fecha  = parsear_fecha(cobro['fecha'])
    agente = cobro['agente_cobro']
    ric    = int(cobro['ric'] or 0)
    # si no hay ric el cobro no tiene deuda de referencia → entrada 0 (no imputar)
    entrada_cobro = entrada if ric != 0 else 0
    params = (
        empresa,
        ejercicio,
        canal,
        cobro['cliente'],
        fecha,
        agente,
        entrada_cobro,
        'CON',
        -(cobro['pago'] or 0.0),   # importe negativo = cobro
        ric,
        cobro['id'],               # registro_palm = id MySQL
        (cobro['tipo'] or '')[:3],
        (cobro['serie'] or '')[:2],
        int(cobro['rig'] or 0),
        0,                         # reparto: siempre 0 desde movil
    )
    # IB 5.6: isc_dsql_execute_immediate omite el prepare (EXECUTE PROCEDURE
    # no soporta prepare en IB 5.6). Se usan valores literales sin '?'.
    con_ib.execute_immediate(ib_exec('E_CARTERA_NUEVO_COBRO', params))


def desmarcar_deudas(con_ib, log, empresa, ejercicio, canal, agentes):
    """Desmarca deudas enviadas para cada agente que tenia cobros.
    No critico: si falla se loguea pero no aborta."""
    for agente in agentes:
        try:
            con_ib.execute_immediate(
                ib_exec('MOV_MARCA_DEUDAS_EN_MOVIL',
                        (empresa, ejercicio, canal, agente, 0))
            )
            con_ib.commit()
            log.info(f'Deudas desmarcadas para agente {agente}.')
        except Exception as exc:
            log.warning(f'No se pudieron desmarcar deudas agente {agente}: {exc}')


def imputar_cobros(con_ib, log, empresa, ejercicio, canal, entrada):
    """Imputa los cobros recien incorporados contra las deudas pendientes.
    Usa fecha de hoy como fecha de trabajo (equivalente a REntorno.FechaTrab)."""
    try:
        con_ib.execute_immediate(
            ib_exec('E_CARTERA_IMPUTA_COBROS',
                    (empresa, ejercicio, canal, entrada,
                     date.today(), FORMA_PAGO_CONTADO))
        )
        con_ib.commit()
        log.info('Cobros imputados correctamente.')
    except Exception as exc:
        log.warning(
            f'No se pudieron imputar los cobros: {exc}. '
            'Debera imputarlos manualmente.'
        )


# ─── Main ─────────────────────────────────────────────────────────────────────

def main(config_path=DEFAULT_CONFIG, dry_run=False):
    from logging.handlers import RotatingFileHandler
    log_path = os.path.join(_here, 'sync_cobros.log')
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s %(levelname)s %(message)s',
        handlers=[
            RotatingFileHandler(log_path, maxBytes=2*1024*1024,
                                backupCount=10, encoding='utf-8'),
            logging.StreamHandler(sys.stdout),
        ],
    )
    log = logging.getLogger(__name__)
    if dry_run:
        log.info('*** MODO DRY-RUN: solo lectura, no se escribe nada ***')
    log.info(f'Driver IB: {DRIVER}')

    cfg = configparser.ConfigParser()
    cfg.read(config_path, encoding='utf-8')

    empresa   = cfg.getint('gestion', 'empresa')
    canal     = cfg.getint('gestion', 'canal')
    ejercicio = date.today().year - 1   # ejercicio = anio actual - 1

    # ── MySQL ─────────────────────────────────────────────────────────────────
    con_my = mysql.connector.connect(
        host     = cfg.get('mysql', 'host'),
        user     = cfg.get('mysql', 'user'),
        password = cfg.get('mysql', 'password'),
        database = cfg.get('mysql', 'database'),
        charset  = 'latin1',
    )
    cur_my = con_my.cursor(dictionary=True)
    log.info('MySQL conectado.')

    # ── InterBase ─────────────────────────────────────────────────────────────
    con_ib = conectar_ib(cfg)
    log.info('InterBase conectado.')

    # Registrar sesion en IB (equivalente al login en Castro)
    if dry_run:
        entrada = 0
        log.info('Entrada IB: 0 (dry-run, no se registra sesion)')
    else:
        entrada = obtener_entrada(con_ib, empresa)
        log.info(f'Entrada IB generada: {entrada}')

    # ── Leer cobros pendientes ────────────────────────────────────────────────
    cur_my.execute(SQL_COBROS_PENDIENTES)
    cobros = cur_my.fetchall()

    if not cobros:
        log.info('Sin cobros pendientes.')
        con_my.close()
        con_ib.close()
        return

    log.info(f'{len(cobros)} cobros pendientes para procesar.')

    # ── Paso 1: incorporar cobros a IB ────────────────────────────────────────
    con_ib.begin()
    cur_ib = con_ib.cursor()

    procesados  = []        # ids incorporados correctamente
    ya_existian = []        # ids que ya estaban en IB
    agentes_con_cobros = set()
    error_fatal = None

    for cobro in cobros:
        id_cobro = cobro['id']
        agente   = cobro['agente_cobro']
        try:
            fecha = parsear_fecha(cobro['fecha'])

            if ya_existe_cobro(cur_ib, empresa, ejercicio, canal,
                               agente, cobro['cliente'], id_cobro, fecha):
                prefix = '[DRY-RUN] ' if dry_run else ''
                log.warning(
                    f'{prefix}Cobro id={id_cobro} ya existe en gestion '
                    f'(cli={cobro["cliente"]} agente={agente}). Omitido.'
                )
                ya_existian.append(id_cobro)
                continue

            if dry_run:
                log.info(
                    f'[DRY-RUN] INCORPORARIA cobro id={id_cobro} '
                    f'cli={cobro["cliente"]} agente={agente} '
                    f'pago={cobro["pago"]} fecha={fecha} '
                    f'doc={cobro["tipo"]}/{cobro["serie"]}/{cobro["rig"]}'
                )
            else:
                incorporar_cobro(con_ib, empresa, ejercicio, canal, entrada, cobro)
                log.info(
                    f'Cobro id={id_cobro} incorporado '
                    f'(cli={cobro["cliente"]} agente={agente} pago={cobro["pago"]})'
                )
            procesados.append(id_cobro)
            agentes_con_cobros.add(agente)

        except Exception as exc:
            import traceback
            log.error(f'Error fatal en cobro id={id_cobro}: {exc}')
            log.error(traceback.format_exc())
            error_fatal = exc
            break

    if error_fatal is not None:
        if not dry_run:
            con_ib.rollback()
        log.error('IB rollback. MySQL sin cambios.')
        con_my.close()
        con_ib.close()
        sys.exit(1)

    if dry_run:
        log.info(
            f'[DRY-RUN] Resumen: incorporaria {len(procesados)}, '
            f'ya existian {len(ya_existian)}. '
            f'Agentes: {sorted(agentes_con_cobros)}. '
            f'Ningun cambio realizado.'
        )
        con_my.close()
        con_ib.close()
        return

    # Commit de la incorporacion
    con_ib.commit()
    cur_ib.close()
    log.info(
        f'IB commit incorporacion OK. '
        f'Incorporados: {len(procesados)}, ya existian: {len(ya_existian)}.'
    )

    # Marcar sync=2 en MySQL para todos los procesados (nuevos + ya existentes)
    todos = procesados + ya_existian
    if todos:
        ids_str = ','.join(str(x) for x in todos)
        cur_my.execute(SQL_MARCAR_SYNC.format(ids=ids_str))
        con_my.commit()
        log.info(f'{len(todos)} cobros marcados sync=2 en MySQL.')

    # ── Paso 2: desmarcar deudas enviadas (no critico) ────────────────────────
    if procesados and agentes_con_cobros:
        desmarcar_deudas(con_ib, log, empresa, ejercicio, canal, agentes_con_cobros)

    # ── Paso 3: imputar cobros contra deudas ─────────────────────────────────
    if procesados:
        imputar_cobros(con_ib, log, empresa, ejercicio, canal, entrada)

    cur_my.close()
    con_my.close()
    con_ib.close()


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser(description='Sync cobros MySQL -> InterBase')
    parser.add_argument('config', nargs='?', default=DEFAULT_CONFIG,
                        help='Ruta al archivo .ini (default: sync_cobros.ini)')
    parser.add_argument('--dry-run', action='store_true',
                        help='Solo muestra lo que haria sin escribir nada')
    args = parser.parse_args()
    main(args.config, dry_run=args.dry_run)
