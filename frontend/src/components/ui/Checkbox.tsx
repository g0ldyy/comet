import type { InputHTMLAttributes } from "react";

interface CheckboxProps extends Omit<InputHTMLAttributes<HTMLInputElement>, "type"> {
  label: string;
}

export function Checkbox({ label, ...props }: CheckboxProps) {
  return (
    <label className="checkbox">
      <input type="checkbox" {...props} />
      <span aria-hidden="true" className="checkbox__control" />
      <span>{label}</span>
    </label>
  );
}
