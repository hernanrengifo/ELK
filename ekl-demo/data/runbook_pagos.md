# Runbook — Servicio de Pagos (pagos-core)

**Propietario técnico:** Laura Gómez (Líder técnico pagos-core)
**Última actualización:** 2026-09-10

## Descripción general

`pagos-core` es el servicio que procesa las transacciones de pago de la
plataforma: liquidación de cash management, débitos y créditos del
producto de crédito rotativo, y notificaciones de confirmación hacia el
cliente. Corre en tres réplicas activas detrás del balanceador interno y
expone sus métricas operativas al dashboard corporativo.

## Dependencias conocidas

La base de datos principal de `pagos-core` es **PAY-DB-01** (Oracle,
ambiente productivo). Todo el estado transaccional del día — turnos de
liquidación, colas de pago pendientes y el libro mayor intradía — vive
en esa base. `pagos-core` también escribe en **PAY-DB-02** como réplica
de solo lectura para reportería, pero PAY-DB-01 es la única fuente de
verdad para escritura.

Un detalle que no siempre es obvio para quien llega nuevo al equipo:
**`nomina-batch` también depende de PAY-DB-01**, aunque nómina tiene su
propio servicio y su propio pipeline batch nocturno. La razón histórica
es que el proceso de nómina liquida los pagos de planilla usando las
mismas tablas de cuentas y turnos de liquidación que usa `pagos-core`
para los pagos del día — nunca se separó esa dependencia cuando se creó
`nomina-batch`. Cualquier ventana de mantenimiento o migración sobre
PAY-DB-01 afecta a los dos servicios al mismo tiempo, no solo a
`pagos-core`.

`notificaciones` depende a su vez de `pagos-core`: solo dispara un
mensaje al cliente después de que `pagos-core` confirma que el pago se
liquidó correctamente. Si `pagos-core` no responde, `notificaciones` no
tiene nada que enviar.

## Cambio programado

Está agendado el cambio **CHG-2026-0917**: migración de PAY-DB-01 de
Oracle a PostgreSQL, con ventana el sábado de 02:00 a 06:00. Dado que
`nomina-batch` comparte la misma base de datos, el equipo de nómina debe
estar en la sala de guardia durante toda la ventana, no solo el equipo
de pagos.

## Contacto de escalamiento

- Servicio: Laura Gómez (pagos-core)
- Base de datos: Marcela Duque (DBA PAY-DB-01 / PAY-DB-02)
- Nómina (impacto colateral): Andrés Felipe Rojas (nomina-batch)
