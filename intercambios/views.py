"""Pantallas de propuesta, detalle y respuesta de un intercambio (US-009 y US-010)."""

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.http import HttpResponseBadRequest
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_GET, require_POST

from cuadrante.models import Turno
from cuentas.roles import es_responsable, solo_empleado
from reglas.compatibilidad import validar_rn03

from .caducidad import caducar_vencidas
from .models import ReservaTurno, SolicitudIntercambio
from .propuestas import (
    IncompatibilidadRN03Error,
    PropuestaIntercambioError,
    proponer_intercambio,
)
from .respuestas import (
    NoEsDestinatarioError,
    SolicitudNoPendienteError,
    responder_propuesta,
)

ACCIONES = {
    "PROPUESTA": "Propuesta del intercambio",
    "ACEPTADA": "Aceptación",
    "RECHAZADA": "Rechazo",
    "APROBADA": "Aprobación",
    "CADUCADA": "Caducidad",
}


def motivos_rn03(empleado, companero):
    """Regla y motivo de cada incumplimiento de RN-03 (US-009-AC-05, NFR-005)."""
    return [
        f"{incumplimiento.regla}: {incumplimiento.mensaje}"
        for incumplimiento in validar_rn03(empleado, companero)
    ]


def problemas_previos(empleado, turno):
    """Motivos por los que no se puede proponer sobre el turno, antes de enseñar el formulario."""
    if turno.empleado_id == empleado.id:
        return ["No puedes proponer un intercambio sobre un turno propio."]
    if turno.inicio <= timezone.now():
        return ["Este turno ya ha empezado y no se puede intercambiar."]
    motivos = motivos_rn03(empleado, turno.empleado)
    if motivos:
        return motivos
    if ReservaTurno.objects.filter(turno=turno).exists():
        return ["Este turno ya forma parte de otra solicitud activa; no está disponible."]
    return []


@solo_empleado
def proponer(request, turno_id):
    """Elegir uno de los turnos propios y confirmar la propuesta (US-009-AC-01)."""
    empleado = getattr(request.user, "empleado", None)
    if empleado is None or empleado.categoria_id is None:
        raise PermissionDenied
    caducar_vencidas()  # libera los turnos de solicitudes vencidas antes de comprobarlos (AC-08)
    turno = get_object_or_404(
        Turno.objects.select_related("empleado__categoria"),
        pk=turno_id,
        empleado__servicio_id=empleado.servicio_id,
        empleado__es_responsable=False,
    )
    mis_turnos = Turno.objects.filter(
        empleado=empleado, inicio__gt=timezone.now(), reserva_intercambio__isnull=True
    ).order_by("inicio")
    errores = problemas_previos(empleado, turno)
    elegido = None

    if request.method == "POST" and not errores:
        elegido = Turno.objects.filter(
            empleado=empleado, pk=request.POST.get("turno_propio") or None
        ).first()
        if elegido is None:
            errores = ["Elige uno de tus turnos para ofrecerlo a cambio."]
        else:
            try:
                solicitud = proponer_intercambio(empleado, elegido, turno)
            except IncompatibilidadRN03Error as error:
                errores = motivos_rn03(empleado, turno.empleado) or [str(error)]
            except PropuestaIntercambioError as error:
                errores = [str(error)]
            else:
                messages.success(
                    request,
                    f"Propuesta enviada a {turno.empleado.nombre}. "
                    "Los dos turnos quedan reservados mientras esté pendiente.",
                )
                return redirect("detalle_solicitud", pk=solicitud.pk)

    return render(
        request,
        "intercambios/proponer.html",
        {
            "turno": turno,
            "mis_turnos": mis_turnos,
            "errores": errores,
            "elegido": elegido,
            "puede_proponer": not problemas_previos(empleado, turno),
        },
    )


def puede_ver(usuario, solicitud):
    """Los dos empleados y el responsable de su servicio (US-009-AC-02, NFR-001)."""
    empleado = getattr(usuario, "empleado", None)
    if empleado is None:
        return False
    if empleado.id in (solicitud.empleado_proponente_id, solicitud.empleado_solicitado_id):
        return True
    return (
        es_responsable(usuario)
        and empleado.es_responsable
        and empleado.servicio_id == solicitud.empleado_proponente.servicio_id
    )


def siguiente_accion(solicitud, empleado):
    Estado = SolicitudIntercambio.Estado
    if solicitud.estado == Estado.PROPUESTA:
        if empleado.id == solicitud.empleado_solicitado_id:
            return "Te toca responder: aceptar o rechazar la propuesta."
        return f"Pendiente de que {solicitud.empleado_solicitado.nombre} la acepte o la rechace."
    if solicitud.estado == Estado.ACEPTADA:
        return "Pendiente de la aprobación del responsable del servicio."
    return "Ninguna: la solicitud ha terminado."


@login_required
@require_GET
def detalle(request, pk):
    """Detalle de la solicitud, también cuando ha terminado (US-009-AC-02, AC-07 y AC-08)."""
    caducar_vencidas()
    solicitud = get_object_or_404(
        SolicitudIntercambio.objects.select_related(
            "turno_ofrecido",
            "turno_solicitado",
            "empleado_proponente",
            "empleado_solicitado",
            "autor_ultima_accion",
        ),
        pk=pk,
    )
    if not puede_ver(request.user, solicitud):
        raise PermissionDenied
    empleado = request.user.empleado
    puede_responder = (
        solicitud.estado == SolicitudIntercambio.Estado.PROPUESTA
        and empleado.id == solicitud.empleado_solicitado_id
    )
    return render(
        request,
        "intercambios/detalle.html",
        {
            "solicitud": solicitud,
            "siguiente": siguiente_accion(solicitud, empleado),
            "ultima_accion": ACCIONES.get(
                solicitud.ultima_accion, solicitud.ultima_accion.capitalize()
            ),
            # La caducidad la aplica el sistema, no la persona guardada como autora.
            "por_el_sistema": solicitud.estado == SolicitudIntercambio.Estado.CADUCADA,
            "puede_responder": puede_responder,
        },
    )


RESPUESTAS = {"aceptar": True, "rechazar": False}


@solo_empleado
@require_POST
def responder(request, pk):
    """Aceptar o rechazar una propuesta desde su detalle (US-010-AC-01 a AC-05)."""
    solicitud = get_object_or_404(SolicitudIntercambio, pk=pk)
    empleado = getattr(request.user, "empleado", None)
    if empleado is None or not puede_ver(request.user, solicitud):
        raise PermissionDenied
    respuesta = request.POST.get("respuesta")
    if respuesta not in RESPUESTAS:
        return HttpResponseBadRequest("Respuesta no válida.")
    acepta = RESPUESTAS[respuesta]
    try:
        solicitud = responder_propuesta(
            empleado, solicitud, acepta, request.POST.get("motivo", "")
        )
    except NoEsDestinatarioError:
        raise PermissionDenied from None
    except SolicitudNoPendienteError:
        solicitud.refresh_from_db()
        messages.error(
            request,
            "Esta solicitud ya no admite respuesta: "
            f"está {solicitud.get_estado_display().lower()}.",
        )
    else:
        if acepta:
            messages.success(
                request,
                "Has aceptado el intercambio. "
                "Queda pendiente de la aprobación del responsable del servicio.",
            )
        else:
            messages.success(
                request,
                "Has rechazado el intercambio. "
                "Los dos turnos vuelven a estar disponibles.",
            )
    return redirect("detalle_solicitud", pk=pk)
