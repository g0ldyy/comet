import * as SelectPrimitive from "@radix-ui/react-select";
import { Check, ChevronDown } from "lucide-react";
import {
  Children,
  isValidElement,
  type OptionHTMLAttributes,
  type ReactElement,
  type ReactNode,
  useId,
  useRef,
} from "react";

interface SelectProps {
  children: ReactNode;
  className?: string;
  disabled?: boolean;
  hint?: string | undefined;
  label: string;
  labelHidden?: boolean;
  leadingIcon?: ReactNode;
  name?: string;
  onValueChange?: (value: string) => void;
  required?: boolean | undefined;
  value: string;
}

interface SelectOption {
  disabled: boolean;
  label: ReactNode;
  value: string;
}

const EMPTY_VALUE = "__comet_empty__";

function getOptions(children: ReactNode): SelectOption[] {
  return Children.toArray(children).flatMap((child) => {
    if (!isValidElement(child) || child.type !== "option") return [];
    const option = child as ReactElement<OptionHTMLAttributes<HTMLOptionElement>>;
    return [
      {
        disabled: option.props.disabled === true,
        label: option.props.children,
        value: String(option.props.value ?? option.props.children ?? ""),
      },
    ];
  });
}

export function Select({
  children,
  className = "",
  disabled = false,
  hint,
  label,
  labelHidden = false,
  leadingIcon,
  name,
  onValueChange,
  required = false,
  value,
}: SelectProps) {
  const id = useId();
  const hintId = hint ? `${id}-hint` : undefined;
  const options = getOptions(children);
  const openedWithPointer = useRef(false);
  const trigger = useRef<HTMLButtonElement>(null);

  return (
    <div className={`field select-field ${className}`.trim()}>
      <label className={labelHidden ? "visually-hidden" : "field__label"} htmlFor={id}>
        {label}
      </label>
      <SelectPrimitive.Root
        disabled={disabled}
        onValueChange={(nextValue) => onValueChange?.(nextValue === EMPTY_VALUE ? "" : nextValue)}
        required={required}
        value={value === "" ? EMPTY_VALUE : value}
        {...(name ? { name } : {})}
      >
        <SelectPrimitive.Trigger
          aria-describedby={hintId}
          aria-label={labelHidden ? label : undefined}
          className="select-trigger"
          id={id}
          onKeyDown={() => {
            openedWithPointer.current = false;
          }}
          onPointerDown={() => {
            openedWithPointer.current = true;
          }}
          ref={trigger}
        >
          {leadingIcon ? (
            <span aria-hidden="true" className="select-trigger__leading-icon">
              {leadingIcon}
            </span>
          ) : null}
          <SelectPrimitive.Value className="select-trigger__value" />
          <SelectPrimitive.Icon asChild>
            <ChevronDown aria-hidden="true" className="select-trigger__icon" size={16} />
          </SelectPrimitive.Icon>
        </SelectPrimitive.Trigger>
        <SelectPrimitive.Portal>
          <SelectPrimitive.Content
            className="select-content"
            collisionPadding={8}
            onCloseAutoFocus={(event) => {
              if (openedWithPointer.current) {
                event.preventDefault();
                trigger.current?.blur();
                openedWithPointer.current = false;
              }
            }}
            onKeyDownCapture={() => {
              openedWithPointer.current = false;
            }}
            position="popper"
            sideOffset={6}
          >
            <SelectPrimitive.Viewport className="select-viewport">
              {options.map((option) => (
                <SelectPrimitive.Item
                  className="select-item"
                  disabled={option.disabled}
                  key={option.value || EMPTY_VALUE}
                  value={option.value || EMPTY_VALUE}
                >
                  <SelectPrimitive.ItemText>{option.label}</SelectPrimitive.ItemText>
                  <SelectPrimitive.ItemIndicator className="select-item__indicator">
                    <Check aria-hidden="true" size={15} strokeWidth={1.6} />
                  </SelectPrimitive.ItemIndicator>
                </SelectPrimitive.Item>
              ))}
            </SelectPrimitive.Viewport>
          </SelectPrimitive.Content>
        </SelectPrimitive.Portal>
      </SelectPrimitive.Root>
      {hint ? (
        <span className="field__hint" id={hintId}>
          {hint}
        </span>
      ) : null}
    </div>
  );
}
