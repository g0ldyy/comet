import { forwardRef, type InputHTMLAttributes, useId } from "react";

interface InputProps extends InputHTMLAttributes<HTMLInputElement> {
  error?: string | undefined;
  hint?: string | undefined;
  label: string;
  labelHidden?: boolean;
}

export const Input = forwardRef<HTMLInputElement, InputProps>(function Input(
  { error, hint, id, label, labelHidden = false, ...props },
  ref,
) {
  const generatedId = useId();
  const inputId = id ?? generatedId;
  const descriptionId = error || hint ? `${inputId}-description` : undefined;

  return (
    <label className="field" htmlFor={inputId}>
      <span className={labelHidden ? "visually-hidden" : "field__label"}>{label}</span>
      <input
        ref={ref}
        aria-describedby={descriptionId}
        aria-invalid={error ? true : undefined}
        className="input"
        id={inputId}
        {...props}
      />
      {error ? (
        <span className="field__error" id={descriptionId}>
          {error}
        </span>
      ) : hint ? (
        <span className="field__hint" id={descriptionId}>
          {hint}
        </span>
      ) : null}
    </label>
  );
});
