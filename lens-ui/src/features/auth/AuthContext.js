import { createContext } from 'react';

/** Shared auth context; kept in its own module for Fast Refresh. */
export const AuthContext = createContext(null);
