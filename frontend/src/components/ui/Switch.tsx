import * as SwitchPrimitive from "@radix-ui/react-switch";
import { useId } from "react";

interface SwitchProps {
  checked: boolean;
  className?: string | undefined;
  compact?: boolean;
  disabled?: boolean;
  label: string;
  onCheckedChange: (checked: boolean) => void;
}

export function Switch({
  checked,
  className,
  compact = false,
  disabled = false,
  label,
  onCheckedChange,
}: SwitchProps) {
  const id = useId();
  const fieldClassName = `switch-field${compact ? " switch-field--compact" : ""}${className ? ` ${className}` : ""}`;
  return (
    <div className={fieldClassName}>
      <label className={compact ? "visually-hidden" : undefined} htmlFor={id}>
        {label}
      </label>
      <SwitchPrimitive.Root
        checked={checked}
        className="switch"
        disabled={disabled}
        id={id}
        onCheckedChange={onCheckedChange}
      >
        <SwitchPrimitive.Thumb className="switch__thumb" />
      </SwitchPrimitive.Root>
    </div>
  );
}
