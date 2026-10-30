# Noticias Financieras — resumen semanal en Discord

Todos los domingos a las 13:00 (hora Argentina) publica en un canal de Discord un resumen de la semana:

- **Resumen ejecutivo** y **highlights** (los 5-8 hechos que movieron el mercado), con enlace a cada fuente.
- **Mercados**: índices, tasas, dólar oficial y CCL, commodities, sectores de EE.UU., ADRs argentinos y grandes de EE.UU. Muestra la variación semanal y mensual, y las acciones que más subieron y bajaron.
- **Macro global** y **Argentina**.
- **Análisis fundamental** de las empresas con novedades: P/E, forward P/E, EV/EBITDA, crecimiento, márgenes, ROE, deuda, free cash flow, precio objetivo y consenso de analistas.
- **Oportunidades para investigar**: tesis, catalizador, riesgo principal y horizonte de cada una.
- **Riesgos** y **agenda** de la semana próxima (eventos y balances de la watchlist).

## Cómo garantiza la fiabilidad

| Mecanismo | Detalle |
|---|---|
| Solo medios reconocidos | Reuters, Bloomberg, WSJ, FT, CNBC, MarketWatch, Barron's, The Economist, Seeking Alpha, Investing.com, Bloomberg Línea, Ámbito, El Cronista, Infobae, La Nación, iProfesional, Clarín (`config.toml`). |
| Ventana estricta | Se descartan las notas de más de 7 días y las que no tienen fecha. |
| Cobertura cruzada | Las notas de una misma historia se agrupan. Las que cubrieron más medios tienen prioridad, y cada punto indica cuántos medios lo respaldan. |
| Cifras de datos, no de la IA | Precios, variaciones y fundamentals salen de Yahoo Finance. Claude solo redacta y analiza sobre esos datos. |
| Citas verificadas | Cada punto tiene que citar IDs de noticias reales. Si no cita ninguna, se descarta. Los enlaces los arma el código, no el modelo, así que no puede inventar URLs. |
| Datos viejos fuera | Se descarta cualquier cotización con más de 5 días de antigüedad. Por ejemplo, hoy Yahoo no tiene el Merval actualizado. |
| Tickers validados | Si Yahoo Finance no reconoce un ticker propuesto por la IA, no aparece. |
| Degradación controlada | Si falla Claude, se publican igual los datos de mercado y los titulares con más cobertura. Si falla todo, llega un aviso al canal. |
| Auditoría | Cada corrida guarda el resumen y los datos crudos (`output/*.json`, `.md`) como artifact de GitHub Actions durante 90 días. |

## Puesta en marcha

1. **Webhook de Discord**: en el canal, *Editar canal → Integraciones → Webhooks → Nuevo webhook → Copiar URL*.
2. **API key de Claude**: se crea en <https://console.anthropic.com/>. Cada resumen cuesta alrededor de US$ 0,50–1 con Claude Opus 5.5.
3. **Secrets en GitHub**: en *Settings → Secrets and variables → Actions*, cargá `ANTHROPIC_API_KEY` y `DISCORD_WEBHOOK_URL`.
4. Subí el repo. El workflow `.github/workflows/resumen-semanal.yml` arranca los domingos a las 12:15 (hora Argentina), genera el resumen y espera hasta las 13:00 para publicarlo. Así compensa las demoras habituales del cron de GitHub.
5. Para probar sin esperar al domingo: *Actions → Resumen financiero semanal → Run workflow*. Con `dry_run` activado lo genera pero no lo publica; el resultado queda como artifact.

> GitHub desactiva los workflows programados de repos **públicos** después de 60 días sin commits. Si pasa, se reactiva desde la pestaña Actions.

## Correrlo localmente

```bash
python -m venv .venv
.venv/Scripts/pip install -r requirements.txt   # en Linux/Mac: .venv/bin/pip
cp .env.example .env                            # completar las claves
python -m noticias_financieras --dry-run        # genera output/resumen-AAAA-MM-DD.md sin publicar
python -m noticias_financieras --dry-run --no-llm   # solo datos y titulares, sin usar Claude
python -m noticias_financieras --no-wait        # publica ya
```

## Personalizar

Todo se ajusta en `config.toml`: fuentes RSS, watchlists por grupo, hora de publicación, modelo y cantidad de empresas a analizar.

---
*Es información, no asesoramiento financiero.*
