import React from 'react';

export default function SimulationIcon({ size = 24, className = '', strokeWidth = 2, ...props }) {
    return (
        <svg
            width={size}
            height={size}
            viewBox="0 0 24 24"
            fill="none"
            stroke="currentColor"
            strokeWidth={strokeWidth}
            strokeLinecap="round"
            strokeLinejoin="round"
            className={className}
            aria-hidden="true"
            {...props}
        >
            <path d="M4 14a8 8 0 0 1 16 0" />
            <path d="M6.7 9.2 8 10.5" />
            <path d="M12 6v2" />
            <path d="m17.3 9.2-1.3 1.3" />
            <path d="m10.8 14.7 3.7-4.6" />
            <circle cx="10.8" cy="14.7" r="1.1" fill="currentColor" stroke="none" />
            <path d="M13.5 15.2v5.1l4.4-2.55-4.4-2.55Z" fill="currentColor" stroke="none" />
        </svg>
    );
}
