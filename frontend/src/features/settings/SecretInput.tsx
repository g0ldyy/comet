import { type ChangeEventHandler, useId, useState } from "react";
import { SecretToggle } from "./SecretToggle";

export function SecretInput({
  disabled,
  error,
  hint,
  label,
  labelHidden = false,
  onChange,
  value,
}: {
  disabled: boolean;
  error?: string | undefined;
  hint?: string | undefined;
  label: string;
  labelHidden?: boolean;
  onChange: ChangeEventHandler<HTMLInputElement>;
  value: string;
}) {
  const id = useId();
  const descriptionId = error || hint ? `${id}-description` : undefined;
  const [revealed, setRevealed] = useState(false);

  return (
    <label className="field" htmlFor={id}>
      <span className={labelHidden ? "visually-hidden" : "field__label"}>{label}</span>
      <span className="secret-input">
        <input
          aria-describedby={descriptionId}
          aria-invalid={error ? true : undefined}
          className="input"
          disabled={disabled}
          id={id}
          onChange={onChange}
          spellCheck={false}
          type={revealed ? "text" : "password"}
          value={value}
        />
        <SecretToggle onToggle={() => setRevealed((current) => !current)} revealed={revealed} />
      </span>
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
}
