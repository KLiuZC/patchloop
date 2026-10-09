export default function ReviewStepHeading({ number, title, children }) {
  return <div className="review-step-heading"><div className="review-step-title"><span className="step-number" aria-hidden="true">{number}</span><h2>{title}</h2></div>{children}</div>
}
