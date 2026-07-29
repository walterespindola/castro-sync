#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
sync_envio.py
Envio directo InterBase -> MySQL de datos para app movil.
Reemplaza el paso IB->BDE->MySQL del envio manual (AEnviarExecute).

Por cada IMEI habilitado (imeis.habilitado=1):
  Preventistas (agente < 5000):
    - cli_clientes, art_articulos, art_precios, cli_deudas
  Repartidores (agente >= 5000):
    - cli_clientes, art_articulos, art_precios, cli_deudas
    - doc_pedidos_cab (via VER_MOV_REP_CAB) + doc_pedidos_det (via VER_MOV_REP_DET)

Uso:
    py -3.12-32 sync_envio.py [ruta_config.ini]
    py -3.12-32 sync_envio.py --dry-run
"""

import sys
import os
import logging
import configparser
import traceback
from datetime import datetime, date

_here = os.path.dirname(os.path.abspath(__file__))
if os.path.isdir(os.path.join(_here, 'fdb')):
    sys.path.insert(0, _here)

import mysql.connector
import fdb

DEFAULT_CONFIG = 'sync_envio.ini'

# Mapeo MODO_IVA (InterBase integer) -> codigo palm
# 0=NC, 1=RI(Responsable Inscripto), 2=CF(Consumidor Final),
# 3=EX(Exento), 4=MN(Monotributo)
IVA_MAP = {0: 'NC', 1: 'RI', 2: 'CF', 3: 'EX', 4: 'MN', 5: 'PE'}

# Mapeo MODO_RENTAS (IB) -> COBRA_DGR (palm) — de GetModoDeRentasEnMovil
DGR_MAP = {0: 0, 1: 1, 2: 2, 3: 7, 4: 8, 5: 5, 6: 4}


# ─── Helpers SQL ─────────────────────────────────────────────────────────────

def ib_str(v):
    """Escapa un valor string para literal SQL IB (para NOT IN / IN)."""
    if v is None:
        return 'NULL'
    return "'" + str(v).replace("'", "''") + "'"

def ib_int_list(values):
    """'1,2,3' para usar en IN (...) con enteros."""
    return ', '.join(str(int(v)) for v in values)

def ib_str_list(values):
    """'A','B','C' para usar en IN (...) con strings."""
    return ', '.join(ib_str(v) for v in values)


# ─── Queries InterBase ────────────────────────────────────────────────────────

SQL_FAM_EXCLUIDAS = "SELECT fam_sys_codigo, fam_baja FROM sys_constantes"

SQL_CLIENTES = """\
    SELECT CLIENTE, TIT_CLIENTE, DIR_COMPLETA, FORMA_PAGO, TIT_LOCALIDAD,
           TELEFONO01, TELEFONO02, NIF, RIESGO_ACT, MODO_IVA, DESCUENTO_CIAL,
           OMITE_DISP, TIPO_IRPF, DIA, RIESGO_AUT, PRIORIDAD, TARIFA,
           VOLUMEN, NOTAS, P_EX_RENTAS, MODO_RENTAS, LATITUD, LONGITUD
    FROM VER_MOV_CLIENTES
    WHERE empresa=? AND ejercicio=? AND canal=? AND agente=?
"""

# Articulos: NOT IN se arma con literales (IB 5.6 acepta ? en SELECT pero
# NOT IN con ? puede ser problematico segun driver; literals son mas seguros).
_SQL_ARTICULOS_TMPL = """\
    SELECT ARTICULO, TIT_ARTICULO, TIT_FAMILIA, PVP, EXISTENCIAS,
           P_BASE_IMPO, P_IVA_PALM, P_RECARGO_PALM, DESCUENTO,
           P_IRPF, P_RENTAS, UDS_X_BULTO, MOV_TOMA_STK
    FROM VER_MOV_ARTICULOS
    WHERE empresa=? AND ejercicio=? AND canal=? AND agente=?
      AND familia NOT IN ({fam_sys},{fam_baja})
      AND sincronizar=1
      AND disponibilidad IN (1,2)
"""

# VER_MOV_PRECIOS ya filtra sincronizar=1 y disponibilidad in (1,2) en la vista
_SQL_PRECIOS_TMPL = """\
    SELECT TARIFA, ARTICULO, PVP
    FROM VER_MOV_PRECIOS
    WHERE empresa=? AND ejercicio=? AND canal=? AND agente=?
      AND familia NOT IN ({fam_sys},{fam_baja})
"""

SQL_DEUDAS = """\
    SELECT COD_CLI_PRO, DOC_TIPO, DOC_SERIE, DOC_NUMERO,
           DOC_FECHA, AGENTE, LIQUIDO, REGISTRO, LINEA
    FROM VER_MOV_DEUDAS
    WHERE empresa=? AND ejercicio=? AND canal=? AND agente=?
"""

# ─── Queries para repartidores ───────────────────────────────────────────────

# Ultimo reparto: transportista = agente-5000, el mas reciente por rig desc
SQL_ULTIMO_REPARTO = """\
    SELECT rig FROM ges_cabeceras_reparto
    WHERE empresa=? AND ejercicio=? AND canal=? AND transportista=?
    ORDER BY rig DESC
"""

# Lista de clientes del reparto via SP (clientes de facturas + clientes con deuda)
SQL_CLIENTES_DEL_REPARTO = \
    "SELECT cliente FROM MOV_CLIENTES_DE_REPARTO(?, ?, ?, ?)"

# Lista de articulos del reparto via detalle de facturas
SQL_ARTICULOS_DEL_REPARTO = """\
    SELECT DISTINCT det.articulo
    FROM ges_cabeceras_s_fac cab
      LEFT JOIN ges_detalles_s det ON det.id_s = cab.id_s
    WHERE cab.empresa=? AND cab.ejercicio=? AND cab.canal=? AND cab.reparto=?
"""

# Clientes del reparto: VER_MOV_REP_CLI con lista de codigos ({in_clientes})
_SQL_CLIENTES_REP_TMPL = """\
    SELECT CLIENTE, TIT_CLIENTE, DIR_COMPLETA, FORMA_PAGO, TIT_LOCALIDAD,
           TELEFONO01, TELEFONO02, NIF, RIESGO_ACT, MODO_IVA, DESCUENTO_CIAL,
           OMITE_DISP, TIPO_IRPF, DIA, RIESGO_AUT, PRIORIDAD, TARIFA,
           VOLUMEN, NOTAS, P_EX_RENTAS, MODO_RENTAS, LATITUD, LONGITUD
    FROM VER_MOV_REP_CLI
    WHERE empresa=? AND ejercicio=? AND canal=? AND cliente IN ({in_clientes})
"""

# Articulos del reparto: VER_MOV_REP_ART con lista de codigos ({in_articulos})
_SQL_ARTICULOS_REP_TMPL = """\
    SELECT ARTICULO, TIT_ARTICULO, TIT_FAMILIA, PVP, EXISTENCIAS,
           P_BASE_IMPO, P_IVA_PALM, P_RECARGO_PALM, DESCUENTO,
           P_IRPF, P_RENTAS, UDS_X_BULTO, MOV_TOMA_STK
    FROM VER_MOV_REP_ART
    WHERE empresa=? AND ejercicio=? AND canal=?
      AND familia NOT IN ({fam_sys},{fam_baja})
      AND sincronizar=1 AND disponibilidad IN (1,2)
      AND articulo IN ({in_articulos})
"""

# Deudas del reparto: VER_MOV_DEUDAS_ALL sin filtro de agente
# (fEnviarSoloLasDeudasDelReparto = false, hardcoded en Delphi)
SQL_DEUDAS_REP = """\
    SELECT COD_CLI_PRO, DOC_TIPO, DOC_SERIE, DOC_NUMERO,
           DOC_FECHA, AGENTE, LIQUIDO, REGISTRO, LINEA
    FROM VER_MOV_DEUDAS_ALL
    WHERE empresa=? AND ejercicio=? AND canal=?
"""

# Cabeceras del reparto (un reparto especifico)
SQL_REP_CABS = """\
    SELECT REPARTO, CANAL, SERIE, RIG, FECHA_HORA, REFERENCIA, CLIENTE,
           BRUTO_VAR, BASEIMPONIBLE_VAR, ING_BRUTOS_VAR, IVA_VAR, LIQUIDO,
           FORMA_PAGO, I_REC_VAR, VENDEDOR, ID_S
    FROM VER_MOV_REP_CAB
    WHERE empresa=? AND ejercicio=? AND canal=? AND reparto=?
"""

# Detalles del reparto
SQL_REP_DETS = """\
    SELECT ID_S, ID_DETALLES_S, CANAL, SERIE, RIG, ARTICULO, TITULO,
           UNIDADES, PRECIO, BRUTO, DESCUENTO, LIQUIDO, B_IMPONIBLE,
           C_IVA, C_RECARGO, I_DESCUENTO, LINEA
    FROM VER_MOV_REP_DET
    WHERE empresa=? AND ejercicio=? AND canal=? AND reparto=?
"""

# ─── Queries MySQL ────────────────────────────────────────────────────────────

SQL_IMEIS = "SELECT imei, agente FROM imeis WHERE habilitado=1 ORDER BY agente"


# ─── Transformaciones ─────────────────────────────────────────────────────────

def mapear_iva(modo_iva):
    return IVA_MAP.get(modo_iva or 0, 'NC')

def mapear_dgr(modo_rentas):
    return DGR_MAP.get(modo_rentas or 0, 0)

def get_direccion(r):
    """Replica GetDireccion(): FORMA_PAGO + '-' + DIR_COMPLETA, max 80 chars."""
    fp  = (r.get('FORMA_PAGO') or '').strip()
    dir = (r.get('DIR_COMPLETA') or '').strip()
    result = fp + '-' + dir
    return result[:80]

def get_telefonos(r):
    """Replica GetTelefonos(): TELEFONO01 + ' ' + TELEFONO02."""
    t1 = (r.get('TELEFONO01') or '').strip()
    t2 = (r.get('TELEFONO02') or '').strip()
    return (t1 + ' ' + t2).strip()

def get_rubro(r):
    """Replica GetRubro(): TIT_FAMILIA; agrega '*' si MOV_TOMA_STK=1."""
    rubro = (r.get('TIT_FAMILIA') or '').strip()
    if r.get('MOV_TOMA_STK') == 1:
        rubro += '*'
    return rubro

def formatear_fecha(d):
    """Devuelve fecha como 'DD/MM/YYYY' para el palm."""
    if d is None:
        return date.today().strftime('%d/%m/%Y')
    if isinstance(d, datetime):
        return d.strftime('%d/%m/%Y')
    if isinstance(d, date):
        return d.strftime('%d/%m/%Y')
    return str(d)

def formatear_fecha_hora(d):
    """Devuelve datetime como 'DD/MM/YYYY HH:MM:SS' para el palm."""
    if d is None:
        return date.today().strftime('%d/%m/%Y 00:00:00')
    if isinstance(d, datetime):
        return d.strftime('%d/%m/%Y %H:%M:%S')
    if isinstance(d, date):
        return d.strftime('%d/%m/%Y 00:00:00')
    return str(d)

def as_float(v):
    if v is None:
        return 0.0
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0

def as_int(v, default=0):
    if v is None:
        return default
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


# ─── Conexiones ───────────────────────────────────────────────────────────────

def conectar_ib(cfg):
    gds32 = cfg.get('interbase', 'gds32',
                    fallback=r'C:\Desarrollo\Castro\Compilacion\gds32.dll')
    con = fdb.connect(
        host            = cfg.get('interbase', 'host'),
        database        = cfg.get('interbase', 'database'),
        user            = cfg.get('interbase', 'user'),
        password        = cfg.get('interbase', 'password'),
        sql_dialect     = 1,
        fb_library_name = gds32,
    )
    return con

def conectar_mysql(cfg):
    return mysql.connector.connect(
        host     = cfg.get('mysql', 'host'),
        user     = cfg.get('mysql', 'user'),
        password = cfg.get('mysql', 'password'),
        database = cfg.get('mysql', 'database'),
        charset  = 'latin1',
    )


# ─── Lectura de constantes IB ─────────────────────────────────────────────────

def leer_familias_excluidas(cur_ib):
    """Lee FAM_SYS_CODIGO y FAM_BAJA desde SYS_CONSTANTES."""
    cur_ib.execute(SQL_FAM_EXCLUIDAS)
    row = cur_ib.fetchone()
    if row:
        return (row[0] or ''), (row[1] or '')
    return '', ''


# ─── Envio de cada entidad ────────────────────────────────────────────────────

def _clean(v):
    # fdb sustituye bytes invalidos de IB con U+FFFD; los descartamos
    # para que el conector MySQL (latin-1) no falle al codificar
    if isinstance(v, str):
        return v.encode('latin-1', errors='ignore').decode('latin-1')
    return v

def _row_dict(cur, row):
    return {d[0]: _clean(v) for d, v in zip(cur.description, row)}


def enviar_clientes(cur_ib, cur_my, con_my,
                    empresa, ejercicio, canal, imei, agente,
                    log, dry_run):
    cur_ib.execute(SQL_CLIENTES, (empresa, ejercicio, canal, agente))
    rows = cur_ib.fetchall()

    if not dry_run:
        cur_my.execute('DELETE FROM cli_clientes WHERE imei=%s', (imei,))

    count = 0
    for raw in rows:
        r = _row_dict(cur_ib, raw)

        iva       = mapear_iva(r.get('MODO_IVA'))
        cobra_dgr = mapear_dgr(r.get('MODO_RENTAS'))
        direccion = get_direccion(r)
        telefonos = get_telefonos(r)
        notas     = (r.get('NOTAS') or '')[:255]

        if dry_run:
            log.info('  [DRY] cli=%s "%s"', r['CLIENTE'], r['TIT_CLIENTE'])
        else:
            cur_my.execute(
                'INSERT INTO cli_clientes '
                '(imei,cliente,denominacion,direccion,localidad,telefonos,cuit_dni,'
                'notas,riesgo,iva,p_desc,omite_disp,cobra_dgr,limite_credito,tarifa,'
                'cant_versiones,categoria,p_dgr_excl,prioridad,sync,latitud,longitud)'
                ' VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,0,%s,%s)',
                (imei,
                 as_int(r.get('CLIENTE')),
                 r.get('TIT_CLIENTE') or '',
                 direccion,
                 r.get('TIT_LOCALIDAD') or '',
                 telefonos,
                 r.get('NIF') or '',
                 notas,
                 as_float(r.get('RIESGO_ACT')),
                 iva,
                 as_float(r.get('DESCUENTO_CIAL')),
                 as_int(r.get('OMITE_DISP')),
                 cobra_dgr,
                 as_float(r.get('RIESGO_AUT')),
                 r.get('TARIFA') or '',
                 as_float(r.get('VOLUMEN')),
                 as_int(r.get('DIA')),
                 as_float(r.get('P_EX_RENTAS')),
                 as_int(r.get('PRIORIDAD')),
                 as_float(r.get('LATITUD')),
                 as_float(r.get('LONGITUD')),
                )
            )
        count += 1

    if not dry_run:
        con_my.commit()
    return count


def enviar_articulos(cur_ib, cur_my, con_my,
                     empresa, ejercicio, canal, imei, agente,
                     fam_sys, fam_baja, log, dry_run):
    sql = _SQL_ARTICULOS_TMPL.format(
        fam_sys=ib_str(fam_sys),
        fam_baja=ib_str(fam_baja),
    )
    cur_ib.execute(sql, (empresa, ejercicio, canal, agente))
    rows = cur_ib.fetchall()

    if not dry_run:
        cur_my.execute('DELETE FROM art_articulos WHERE imei=%s', (imei,))

    count = 0
    for raw in rows:
        r = _row_dict(cur_ib, raw)

        rubro = get_rubro(r)
        # p_ing_brut: P_RENTAS tiene prioridad sobre P_IRPF (rentas sobreescribe)
        p_ing_brut = as_float(r.get('P_RENTAS')) or as_float(r.get('P_IRPF'))

        if dry_run:
            log.info('  [DRY] art=%s "%s" pvp=%s', r['ARTICULO'], r['TIT_ARTICULO'], r['PVP'])
        else:
            cur_my.execute(
                'INSERT INTO art_articulos '
                '(imei,articulo,titulo,precio,stock_ini,stock_ven,id_rubro,rubro,'
                'b_imp,p_iva,p_iva_rec,p_descuento,p_ing_brut,sync,uds_x_bulto)'
                ' VALUES (%s,%s,%s,%s,%s,0,0,%s,%s,%s,%s,%s,%s,0,%s)',
                (imei,
                 r.get('ARTICULO') or '',
                 r.get('TIT_ARTICULO') or '',
                 as_float(r.get('PVP')),
                 as_float(r.get('EXISTENCIAS')),
                 rubro,
                 as_float(r.get('P_BASE_IMPO')),
                 as_float(r.get('P_IVA_PALM')),
                 as_float(r.get('P_RECARGO_PALM')),
                 as_float(r.get('DESCUENTO')),
                 p_ing_brut,
                 as_float(r.get('UDS_X_BULTO')),
                )
            )
        count += 1

    if not dry_run:
        con_my.commit()
    return count


def enviar_precios(cur_ib, cur_my, con_my,
                   empresa, ejercicio, canal, imei, agente,
                   fam_sys, fam_baja, log, dry_run):
    sql = _SQL_PRECIOS_TMPL.format(
        fam_sys=ib_str(fam_sys),
        fam_baja=ib_str(fam_baja),
    )
    cur_ib.execute(sql, (empresa, ejercicio, canal, agente))
    rows = cur_ib.fetchall()

    if not dry_run:
        cur_my.execute('DELETE FROM art_precios WHERE imei=%s', (imei,))

    count = 0
    for raw in rows:
        r = _row_dict(cur_ib, raw)

        if dry_run:
            log.info('  [DRY] tar=%s art=%s pvp=%s', r['TARIFA'], r['ARTICULO'], r['PVP'])
        else:
            cur_my.execute(
                'INSERT INTO art_precios '
                '(imei,tarifa,articulo,linea,uds_min,uds_max,precio,descuento,sync)'
                ' VALUES (%s,%s,%s,1,-9999,9999,%s,0,0)',
                (imei,
                 r.get('TARIFA') or '',
                 r.get('ARTICULO') or '',
                 as_float(r.get('PVP')),
                )
            )
        count += 1

    if not dry_run:
        con_my.commit()
    return count


def enviar_deudas(cur_ib, cur_my, con_my,
                  empresa, ejercicio, canal, imei, agente,
                  log, dry_run):
    cur_ib.execute(SQL_DEUDAS, (empresa, ejercicio, canal, agente))
    rows = cur_ib.fetchall()

    if not dry_run:
        cur_my.execute('DELETE FROM cli_deudas WHERE imei=%s', (imei,))

    count = 0
    for raw in rows:
        r = _row_dict(cur_ib, raw)

        fecha  = formatear_fecha(r.get('DOC_FECHA'))
        origen = str(as_int(r.get('AGENTE')))   # agente como string (para rendicion)

        if dry_run:
            log.info('  [DRY] cli=%s %s/%s/%s saldo=%s',
                     r['COD_CLI_PRO'], r['DOC_TIPO'], r['DOC_SERIE'],
                     r['DOC_NUMERO'], r['LIQUIDO'])
        else:
            cur_my.execute(
                'INSERT INTO cli_deudas '
                '(imei,cliente,tipo,serie,rig,fecha,origen,saldo,ric,linea,sync)'
                ' VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,0)',
                (imei,
                 as_int(r.get('COD_CLI_PRO')),
                 r.get('DOC_TIPO') or '',
                 r.get('DOC_SERIE') or '',
                 as_int(r.get('DOC_NUMERO')),
                 fecha,
                 origen,
                 as_float(r.get('LIQUIDO')),
                 as_int(r.get('REGISTRO')),
                 as_int(r.get('LINEA')),
                )
            )
        count += 1

    if not dry_run:
        con_my.commit()
    return count


def obtener_ultimo_reparto(cur_ib, empresa, ejercicio, canal, agente):
    """Devuelve el rig del ultimo reparto del agente.

    GES_CABECERAS_REPARTO.transportista = agente - 5000.
    El 'ultimo' es el de RIG mas alto (ORDER BY RIG DESC, primer registro).
    Replica GetRepartosDeAgente() que sale al primer item de la lista.
    """
    cur_ib.execute(SQL_ULTIMO_REPARTO,
                   (empresa, ejercicio, canal, agente - 5000))
    row = cur_ib.fetchone()
    return as_int(row[0]) if row else None


def enviar_clientes_rep(cur_ib, cur_my, con_my,
                        empresa, ejercicio, canal, imei, clientes,
                        log, dry_run):
    """Envia clientes del reparto desde VER_MOV_REP_CLI.

    clientes: lista de codigos INTEGER obtenida de MOV_CLIENTES_DE_REPARTO.
    Replica la logica de UDMSyncClientes con fReparto > -1.
    """
    if not clientes:
        if not dry_run:
            cur_my.execute('DELETE FROM cli_clientes WHERE imei=%s', (imei,))
            con_my.commit()
        return 0

    sql = _SQL_CLIENTES_REP_TMPL.format(in_clientes=ib_int_list(clientes))
    cur_ib.execute(sql, (empresa, ejercicio, canal))
    rows = cur_ib.fetchall()

    if not dry_run:
        cur_my.execute('DELETE FROM cli_clientes WHERE imei=%s', (imei,))

    count = 0
    for raw in rows:
        r = _row_dict(cur_ib, raw)
        iva       = mapear_iva(r.get('MODO_IVA'))
        cobra_dgr = mapear_dgr(r.get('MODO_RENTAS'))
        direccion = get_direccion(r)
        telefonos = get_telefonos(r)
        notas     = (r.get('NOTAS') or '')[:255]

        if dry_run:
            log.info('  [DRY] cli=%s "%s"', r['CLIENTE'], r['TIT_CLIENTE'])
        else:
            cur_my.execute(
                'INSERT INTO cli_clientes '
                '(imei,cliente,denominacion,direccion,localidad,telefonos,cuit_dni,'
                'notas,riesgo,iva,p_desc,omite_disp,cobra_dgr,limite_credito,tarifa,'
                'cant_versiones,categoria,p_dgr_excl,prioridad,sync,latitud,longitud)'
                ' VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,0,%s,%s)',
                (imei,
                 as_int(r.get('CLIENTE')),
                 r.get('TIT_CLIENTE') or '',
                 direccion,
                 r.get('TIT_LOCALIDAD') or '',
                 telefonos,
                 r.get('NIF') or '',
                 notas,
                 as_float(r.get('RIESGO_ACT')),
                 iva,
                 as_float(r.get('DESCUENTO_CIAL')),
                 as_int(r.get('OMITE_DISP')),
                 cobra_dgr,
                 as_float(r.get('RIESGO_AUT')),
                 r.get('TARIFA') or '',
                 as_float(r.get('VOLUMEN')),
                 as_int(r.get('DIA')),
                 as_float(r.get('P_EX_RENTAS')),
                 as_int(r.get('PRIORIDAD')),
                 as_float(r.get('LATITUD')),
                 as_float(r.get('LONGITUD')),
                )
            )
        count += 1

    if not dry_run:
        con_my.commit()
    return count


def enviar_articulos_rep(cur_ib, cur_my, con_my,
                         empresa, ejercicio, canal, imei, articulos,
                         fam_sys, fam_baja, log, dry_run):
    """Envia articulos del reparto desde VER_MOV_REP_ART.

    articulos: lista de codigos STRING obtenida de ges_detalles_s del reparto.
    Replica UDMSyncArticulos con fReparto > -1 usando VER_MOV_REP_ART.
    """
    if not articulos:
        if not dry_run:
            cur_my.execute('DELETE FROM art_articulos WHERE imei=%s', (imei,))
            con_my.commit()
        return 0

    sql = _SQL_ARTICULOS_REP_TMPL.format(
        fam_sys=ib_str(fam_sys),
        fam_baja=ib_str(fam_baja),
        in_articulos=ib_str_list(articulos),
    )
    cur_ib.execute(sql, (empresa, ejercicio, canal))
    rows = cur_ib.fetchall()

    if not dry_run:
        cur_my.execute('DELETE FROM art_articulos WHERE imei=%s', (imei,))

    count = 0
    for raw in rows:
        r = _row_dict(cur_ib, raw)
        rubro      = get_rubro(r)
        p_ing_brut = as_float(r.get('P_RENTAS')) or as_float(r.get('P_IRPF'))

        if dry_run:
            log.info('  [DRY] art=%s "%s" pvp=%s',
                     r['ARTICULO'], r['TIT_ARTICULO'], r['PVP'])
        else:
            cur_my.execute(
                'INSERT INTO art_articulos '
                '(imei,articulo,titulo,precio,stock_ini,stock_ven,id_rubro,rubro,'
                'b_imp,p_iva,p_iva_rec,p_descuento,p_ing_brut,sync,uds_x_bulto)'
                ' VALUES (%s,%s,%s,%s,%s,0,0,%s,%s,%s,%s,%s,%s,0,%s)',
                (imei,
                 r.get('ARTICULO') or '',
                 r.get('TIT_ARTICULO') or '',
                 as_float(r.get('PVP')),
                 as_float(r.get('EXISTENCIAS')),
                 rubro,
                 as_float(r.get('P_BASE_IMPO')),
                 as_float(r.get('P_IVA_PALM')),
                 as_float(r.get('P_RECARGO_PALM')),
                 as_float(r.get('DESCUENTO')),
                 p_ing_brut,
                 as_float(r.get('UDS_X_BULTO')),
                )
            )
        count += 1

    if not dry_run:
        con_my.commit()
    return count


def enviar_deudas_rep(cur_ib, cur_my, con_my,
                      empresa, ejercicio, canal, imei,
                      log, dry_run):
    """Envia TODAS las deudas (sin filtro de agente) para un repartidor.

    Usa VER_MOV_DEUDAS_ALL porque fEnviarSoloLasDeudasDelReparto = false
    (hardcoded en UDMSyncDeudas.pas linea 163).
    """
    cur_ib.execute(SQL_DEUDAS_REP, (empresa, ejercicio, canal))
    rows = cur_ib.fetchall()

    if not dry_run:
        cur_my.execute('DELETE FROM cli_deudas WHERE imei=%s', (imei,))

    count = 0
    for raw in rows:
        r = _row_dict(cur_ib, raw)
        fecha  = formatear_fecha(r.get('DOC_FECHA'))
        origen = str(as_int(r.get('AGENTE')))

        if dry_run:
            log.info('  [DRY] deu cli=%s %s/%s/%s saldo=%s',
                     r['COD_CLI_PRO'], r['DOC_TIPO'], r['DOC_SERIE'],
                     r['DOC_NUMERO'], r['LIQUIDO'])
        else:
            cur_my.execute(
                'INSERT INTO cli_deudas '
                '(imei,cliente,tipo,serie,rig,fecha,origen,saldo,ric,linea,sync)'
                ' VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,0)',
                (imei,
                 as_int(r.get('COD_CLI_PRO')),
                 r.get('DOC_TIPO') or '',
                 r.get('DOC_SERIE') or '',
                 as_int(r.get('DOC_NUMERO')),
                 fecha,
                 origen,
                 as_float(r.get('LIQUIDO')),
                 as_int(r.get('REGISTRO')),
                 as_int(r.get('LINEA')),
                )
            )
        count += 1

    if not dry_run:
        con_my.commit()
    return count


def enviar_facturas(cur_ib, cur_my, con_my,
                    empresa, ejercicio, canal, imei, reparto,
                    log, dry_run):
    """Envia cabeceras y detalles del reparto especificado.

    Recibe el rig del reparto (obtenido por obtener_ultimo_reparto).
    doc_pedidos_cab.empresa almacena cant_detalles por cab (convencion Delphi).
    precio en det = PRECIO + C_IVA/UNIDADES (GetPrecio de proyecto Castro).
    """
    cur_ib.execute(SQL_REP_CABS, (empresa, ejercicio, canal, reparto))
    cab_rows = cur_ib.fetchall()
    cabs = [_row_dict(cur_ib, raw) for raw in cab_rows]

    if not dry_run:
        cur_my.execute('DELETE FROM doc_pedidos_cab WHERE imei=%s', (imei,))
        cur_my.execute('DELETE FROM doc_pedidos_det WHERE imei=%s', (imei,))

    n_cab = 0
    n_det = 0

    for r in cabs:
        id_s = as_int(r.get('ID_S'))

        # cant_detalles por cab (convension Delphi: se guarda en campo empresa)
        cur_ib.execute(
            'SELECT COUNT(*) FROM ges_detalles_s WHERE id_s=?', (id_s,))
        row = cur_ib.fetchone()
        cant_dets = as_int(row[0]) if row else 0

        fecha = formatear_fecha_hora(r.get('FECHA_HORA'))

        if dry_run:
            log.info('  [DRY] cab rep=%d rig=%s cli=%s liq=%.2f dets=%d',
                     reparto, r.get('RIG'), r.get('CLIENTE'),
                     as_float(r.get('LIQUIDO')), cant_dets)
        else:
            cur_my.execute(
                'INSERT INTO doc_pedidos_cab'
                ' (imei,agente,serie,tipo,rig,anulada,fecha,referencia,cliente,'
                '  bruto_tab,bruto_var,i_desc_tab,i_desc_var,i_iva_tab,i_iva_var,'
                '  liquido,forma_pago,i_ing_brut_tab,i_ing_brut_var,i_b_imp_tab,'
                '  i_b_imp_var,i_rec_iva_tab,i_rec_iva_var,tipo_iva,direccion,'
                '  empresa,sync,_id)'
                ' VALUES (%s,%s,%s,"FAC",%s,0,%s,%s,%s,'
                '         0,%s,0,0,0,%s,'
                '         %s,%s,0,%s,0,'
                '         %s,0,%s,"",0,'
                '         %s,0,%s)',
                (imei, as_int(r.get('VENDEDOR')), r.get('SERIE') or '',
                 as_int(r.get('RIG')), fecha,
                 r.get('REFERENCIA') or '', as_int(r.get('CLIENTE')),
                 as_float(r.get('BRUTO_VAR')), as_float(r.get('IVA_VAR')),
                 as_float(r.get('LIQUIDO')), r.get('FORMA_PAGO') or '',
                 as_float(r.get('ING_BRUTOS_VAR')),
                 as_float(r.get('BASEIMPONIBLE_VAR')),
                 as_float(r.get('I_REC_VAR')),
                 cant_dets, id_s,
                )
            )
        n_cab += 1

    cur_ib.execute(SQL_REP_DETS, (empresa, ejercicio, canal, reparto))
    det_rows = cur_ib.fetchall()
    dets = [_row_dict(cur_ib, raw) for raw in det_rows]

    for d in dets:
        unidades = as_float(d.get('UNIDADES'))
        c_iva    = as_float(d.get('C_IVA'))
        precio   = as_float(d.get('PRECIO')) + (c_iva / unidades if unidades else 0.0)

        if dry_run:
            log.info('  [DRY] det art=%s "%s" u=%.0f p=%.4f',
                     d.get('ARTICULO'), d.get('TITULO'), unidades, precio)
        else:
            cur_my.execute(
                'INSERT INTO doc_pedidos_det'
                ' (imei,serie,tipo,rig,articulo,titulo,indrubro,unidades,precio,'
                '  subtotal1,p_descuento,subtotal,i_b_imp,i_iva,i_iva_rec,i_desc,'
                '  i_ing_brut,sync,_id,id_cab,linea)'
                ' VALUES (%s,%s,"FAC",%s,%s,%s,-1,%s,%s,'
                '         %s,%s,%s,%s,%s,%s,%s,'
                '         0,0,%s,%s,%s)',
                (imei, d.get('SERIE') or '',
                 as_int(d.get('RIG')), d.get('ARTICULO') or '',
                 d.get('TITULO') or '', unidades, precio,
                 as_float(d.get('BRUTO')), as_float(d.get('DESCUENTO')),
                 as_float(d.get('LIQUIDO')), as_float(d.get('B_IMPONIBLE')),
                 c_iva, as_float(d.get('C_RECARGO')),
                 as_float(d.get('I_DESCUENTO')),
                 as_int(d.get('ID_DETALLES_S')), as_int(d.get('ID_S')),
                 as_int(d.get('LINEA')),
                )
            )
        n_det += 1

    if not dry_run:
        con_my.commit()
    return n_cab, n_det


# ─── Main ─────────────────────────────────────────────────────────────────────

def main(config_path=DEFAULT_CONFIG, dry_run=False):
    from logging.handlers import RotatingFileHandler
    log_path = os.path.join(_here, 'sync_envio.log')
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

    cfg = configparser.ConfigParser()
    cfg.read(config_path, encoding='utf-8')

    empresa   = cfg.getint('gestion', 'empresa')
    canal     = cfg.getint('gestion', 'canal')
    ejercicio = date.today().year - 1

    log.info('empresa=%d canal=%d ejercicio=%d', empresa, canal, ejercicio)

    # ── Conexiones ─────────────────────────────────────────────────────────────
    con_my = conectar_mysql(cfg)
    cur_my = con_my.cursor()
    log.info('MySQL conectado.')

    con_ib = conectar_ib(cfg)
    cur_ib = con_ib.cursor()
    log.info('InterBase conectado.')

    # Leer familias excluidas del catalogo
    fam_sys, fam_baja = leer_familias_excluidas(cur_ib)
    log.info('Familias excluidas: sys=%r baja=%r', fam_sys, fam_baja)

    # ── Leer IMEIs habilitados desde MySQL ─────────────────────────────────────
    cur_my.execute(SQL_IMEIS)
    imeis = cur_my.fetchall()

    if not imeis:
        log.info('Sin IMEIs habilitados.')
        cur_ib.close(); con_ib.close()
        cur_my.close(); con_my.close()
        return

    log.info('%d IMEIs habilitados.', len(imeis))

    # ── Procesar por IMEI ─────────────────────────────────────────────────────
    for imei, agente in imeis:
        es_repartidor = (agente >= 5000)
        tipo = 'repartidor' if es_repartidor else 'preventista'
        log.info('--- IMEI=%s agente=%s (%s) ---', imei, agente, tipo)

        try:
            if es_repartidor:
                reparto = obtener_ultimo_reparto(
                    cur_ib, empresa, ejercicio, canal, agente)
                if reparto is None:
                    log.warning('  IMEI %s agente=%d: sin repartos en BD', imei, agente)
                    continue
                log.info('  Ultimo reparto: %d', reparto)

                clientes  = []
                articulos = []
                cur_ib.execute(SQL_CLIENTES_DEL_REPARTO,
                               (empresa, ejercicio, canal, reparto))
                clientes = [row[0] for row in cur_ib.fetchall()]

                cur_ib.execute(SQL_ARTICULOS_DEL_REPARTO,
                               (empresa, ejercicio, canal, reparto))
                articulos = [row[0] for row in cur_ib.fetchall()
                             if row[0] is not None]

                log.info('  Reparto %d: %d clientes, %d articulos',
                         reparto, len(clientes), len(articulos))

                n_cli = enviar_clientes_rep(cur_ib, cur_my, con_my,
                                            empresa, ejercicio, canal, imei,
                                            clientes, log, dry_run)
                n_art = enviar_articulos_rep(cur_ib, cur_my, con_my,
                                             empresa, ejercicio, canal, imei,
                                             articulos, fam_sys, fam_baja,
                                             log, dry_run)
                n_pre = enviar_precios(cur_ib, cur_my, con_my,
                                       empresa, ejercicio, canal, imei, agente,
                                       fam_sys, fam_baja, log, dry_run)
                n_deu = enviar_deudas_rep(cur_ib, cur_my, con_my,
                                          empresa, ejercicio, canal, imei,
                                          log, dry_run)
                n_cab, n_det = enviar_facturas(cur_ib, cur_my, con_my,
                                               empresa, ejercicio, canal, imei,
                                               reparto, log, dry_run)
                log.info('  IMEI %s OK: %d cli / %d arts / %d prec / %d deudas'
                         ' / %d cabs / %d dets',
                         imei, n_cli, n_art, n_pre, n_deu, n_cab, n_det)
            else:
                n_cli = enviar_clientes(cur_ib, cur_my, con_my,
                                        empresa, ejercicio, canal, imei, agente,
                                        log, dry_run)
                n_art = enviar_articulos(cur_ib, cur_my, con_my,
                                         empresa, ejercicio, canal, imei, agente,
                                         fam_sys, fam_baja, log, dry_run)
                n_pre = enviar_precios(cur_ib, cur_my, con_my,
                                       empresa, ejercicio, canal, imei, agente,
                                       fam_sys, fam_baja, log, dry_run)
                n_deu = enviar_deudas(cur_ib, cur_my, con_my,
                                      empresa, ejercicio, canal, imei, agente,
                                      log, dry_run)
                log.info('  IMEI %s OK: %d cli / %d arts / %d prec / %d deudas',
                         imei, n_cli, n_art, n_pre, n_deu)

        except Exception as exc:
            log.error('Error procesando IMEI=%s: %s', imei, exc)
            log.error(traceback.format_exc())
            if not dry_run:
                try:
                    con_my.rollback()
                except Exception:
                    pass

    cur_ib.close()
    con_ib.close()
    cur_my.close()
    con_my.close()
    log.info('=== FIN ===')


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser(description='Sync envio IB->MySQL para app movil')
    parser.add_argument('config', nargs='?', default=DEFAULT_CONFIG,
                        help='Ruta al .ini (default: sync_envio.ini)')
    parser.add_argument('--dry-run', action='store_true',
                        help='Solo muestra lo que haria sin escribir nada')
    args = parser.parse_args()
    main(args.config, dry_run=args.dry_run)
