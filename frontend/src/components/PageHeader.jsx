/**
 * 统一页头：眉题 + 主标题 + 说明 + 右侧操作区
 */
export default function PageHeader({ eyebrow, title, description, actions, children }) {
  return (
    <div className="page-header">
      <div className="flex flex-wrap items-end justify-between gap-4">
        <div className="min-w-0 max-w-2xl">
          {eyebrow ? <p className="page-eyebrow">{eyebrow}</p> : null}
          <h1 className="page-title">{title}</h1>
          {description ? (
            <p className="page-subtitle">{description}</p>
          ) : null}
          {children}
        </div>
        {actions ? (
          <div className="flex flex-wrap items-center gap-2 shrink-0">{actions}</div>
        ) : null}
      </div>
      <div
        className="mt-5 h-px w-full max-w-md"
        style={{
          background:
            'linear-gradient(90deg, rgba(31,122,76,0.45), rgba(31,122,76,0.08), transparent)',
        }}
        aria-hidden
      />
    </div>
  )
}
