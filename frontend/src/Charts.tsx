// Coordinate transforms only. All cash values and probabilities come from Python.
const format = (value: number) => value.toLocaleString('en-US', { maximumFractionDigits: 0 })
const stamp = (value: string) => Date.parse(`${value}T00:00:00Z`)
export function CashChart({ series }: { series: { name: string; color: string; points: { date: string; balance: number }[] }[] }) {
  const values = series.flatMap((s) => s.points.map((p) => p.balance))
  if (!values.length) return <p>No cash path available.</p>
  const dates = [...new Set(series.flatMap((s) => s.points.map((p) => p.date)))].sort()
  const low = Math.min(0, ...values), high = Math.max(0, ...values)
  const y = (value: number) => 190 - (value - low) / (high - low || 1) * 150
  const first = stamp(dates[0]), last = stamp(dates[dates.length - 1])
  const x = (day: string) => 74 + (stamp(day) - first) / (last - first || 1) * 420
  return <figure className="cash-chart">
    <svg viewBox="0 0 540 245" role="img" aria-label="Forecast cash in GEL: likely, cautious and no-payment scenarios">
      {[low, 0, high].filter((v, i, all) => all.indexOf(v) === i).map((v) => <g key={v}>
        <line x1="74" x2="500" y1={y(v)} y2={y(v)} stroke={v === 0 ? '#64748b' : '#dbe3ec'} strokeDasharray={v === 0 ? '5 4' : undefined} />
        <text x="65" y={y(v) + 4} textAnchor="end" fontSize="10" fill="#475569">{format(v)}</text>
      </g>)}
      {series.map((s) => <polyline key={s.name} fill="none" stroke={s.color} strokeWidth="3"
        points={s.points.map((p) => `${x(p.date)},${y(p.balance)}`).join(' ')} />)}
      {dates.filter((_,i) => i === 0 || i === Math.floor(dates.length / 2) || i === dates.length-1).map((day) =>
        <text key={day} x={x(day)} y="215" textAnchor="middle" fontSize="10" fill="#475569">{day.slice(5)}</text>)}
      <text x="74" y="18" fontSize="11" fill="#475569">Recorded / forecast cash · GEL</text>
    </svg>
    <figcaption>{series.map((s) => <span key={s.name}><i style={{ background: s.color }} />{s.name}</span>)}</figcaption>
  </figure>
}

export function ShareBar({ label, value, detail, warning = false }: { label: string; value: number; detail: string; warning?: boolean }) {
  return <div className="share-row"><div><strong>{label}</strong><span>{detail}</span></div>
    <div className="bar-track" role="img" aria-label={`${label}: ${value}%`}>
      <div style={{ width: `${Math.max(0, Math.min(100, value))}%`, background: warning ? '#d97706' : '#2563eb' }} />
    </div>
  </div>
}
