//! Expression parser: text → AST. Work order B1
//! (`docs/work-orders.md`, "B1 · Parser and AST").
//!
//! Grammar, precedence high → low:
//!
//! ```text
//! expr    := add
//! add     := mul (("+" | "-" | "−") mul)*                     left-assoc
//! mul     := unary (("*" | "·" | "⋅" | "×" | "/" | "÷") unary)*  left-assoc
//! unary   := ("+" | "-" | "−" | "√") unary | pow
//! pow     := primary ("^" unary)?                             right-assoc
//! primary := number | "pi" | "π" | "e" | ident
//!          | func "(" expr ")" | "(" expr ")"
//! number  := digit+ ("." digit+)?
//! ```
//!
//! Documented decisions (each is a deliberate choice, not an accident):
//!
//! - `-x^2` parses as `-(x^2)`, matching standard algebraic convention and the
//!   precedence table above. `(-x)^2` requires explicit parentheses. (The
//!   pre-rewrite parser did the opposite; its own doc comment said exponentiation
//!   binds tighter, so the code contradicted its documentation.)
//! - `^` is right-associative: `2^3^2` = `2^(3^2)`. The right operand of `^` is
//!   parsed at the `unary` level so `2^-3` and `2^3^2` both work.
//! - There is no rational literal syntax. `3/4` parses as
//!   `Div(Integer(3), Integer(4))`; B2 canonicalisation may normalise that to a
//!   rational form. This keeps the grammar unambiguous.
//! - `pi`, `π` and `e` are reserved constants; they cannot be used as variable
//!   names. `e` is a constant, so scientific notation for decimal literals
//!   (`1e5`) is deliberately not supported — it would be ambiguous with `1*e`.
//! - Identifiers are ASCII (`[A-Za-z_][A-Za-z0-9_]*`). The only unicode accepted
//!   is the operator/constant set listed above (`·` U+00B7, `⋅` U+22C5, `×`
//!   U+00D7, `÷` U+00F7, `−` U+2212, `√` U+221A, `π` U+03C0). `√` binds exactly
//!   like unary minus: `√x^2` = `√(x^2)`.
//! - The only functions are `sin cos tan exp ln sqrt abs`, each of exactly one
//!   argument. Unknown functions and wrong arities are parse errors.
//! - Implicit multiplication (`2x`, `2(x+1)`) is not supported and produces a
//!   specific error suggesting `*`.
//! - Integers are `i64`; out-of-range integer literals are errors, not silent
//!   wraps. Decimals are non-negative finite `f64` by construction (a leading
//!   minus is a `UnaryOp::Neg`); `print` formats integral decimals as `2.0` so
//!   they re-parse as decimals.
//! - Recursion depth is capped at [`MAX_DEPTH`]; pathological nesting produces a
//!   parse error instead of a stack overflow.

use std::fmt;

/// Maximum parser recursion depth. Exceeding it is a parse error, never a
/// stack overflow. 500 paren levels recurse through ~6 frames per level,
/// well inside a default 2 MiB test-thread stack.
const MAX_DEPTH: usize = 500;

/// A mathematical expression.
#[derive(Debug, Clone, PartialEq)]
pub enum Expr {
    /// Integer literal, e.g. `42`.
    Integer(i64),
    /// Decimal literal, e.g. `2.5`. Always non-negative and finite; a leading
    /// minus is represented as [`UnaryOp::Neg`].
    Decimal(f64),
    /// Named constant (`pi`/`π` or `e`).
    Constant(Const),
    /// Named variable.
    Variable(String),
    /// Binary operation.
    BinOp {
        op: BinOp,
        left: Box<Expr>,
        right: Box<Expr>,
    },
    /// Unary prefix operation (`-x`, `+x`).
    UnaryOp { op: UnaryOp, operand: Box<Expr> },
    /// Function application; every supported function takes exactly one
    /// argument, so `args` always has length 1.
    Call { func: Function, args: Vec<Expr> },
}

/// Binary operators.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum BinOp {
    Add,
    Sub,
    Mul,
    Div,
    Pow,
}

impl BinOp {
    /// The canonical printable form of this operator (what [`print`] emits).
    pub fn symbol(self) -> &'static str {
        match self {
            BinOp::Add => "+",
            BinOp::Sub => "-",
            BinOp::Mul => "*",
            BinOp::Div => "/",
            BinOp::Pow => "^",
        }
    }
}

/// Unary prefix operators.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum UnaryOp {
    Neg,
    Pos,
}

impl UnaryOp {
    /// The canonical printable form of this operator (what [`print`] emits).
    pub fn symbol(self) -> &'static str {
        match self {
            UnaryOp::Neg => "-",
            UnaryOp::Pos => "+",
        }
    }
}

/// Named constants recognised by the parser.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Const {
    Pi,
    E,
}

impl Const {
    /// The canonical printable name (what [`print`] emits).
    pub fn name(self) -> &'static str {
        match self {
            Const::Pi => "pi",
            Const::E => "e",
        }
    }
}

/// The closed set of supported functions.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Function {
    Sin,
    Cos,
    Tan,
    Exp,
    Ln,
    Sqrt,
    Abs,
}

impl Function {
    /// Look up a function by its input-text name.
    pub fn from_name(name: &str) -> Option<Function> {
        match name {
            "sin" => Some(Function::Sin),
            "cos" => Some(Function::Cos),
            "tan" => Some(Function::Tan),
            "exp" => Some(Function::Exp),
            "ln" => Some(Function::Ln),
            "sqrt" => Some(Function::Sqrt),
            "abs" => Some(Function::Abs),
            _ => None,
        }
    }

    /// The canonical printable name (what [`print`] emits).
    pub fn name(self) -> &'static str {
        match self {
            Function::Sin => "sin",
            Function::Cos => "cos",
            Function::Tan => "tan",
            Function::Exp => "exp",
            Function::Ln => "ln",
            Function::Sqrt => "sqrt",
            Function::Abs => "abs",
        }
    }
}

/// A parse failure. `position` is a 0-based **character** offset into the
/// input string (the lexer works on `char`s, not bytes).
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ParseError {
    pub message: String,
    pub position: usize,
}

impl fmt::Display for ParseError {
    fn fmt(&self, f: &mut fmt::Formatter) -> fmt::Result {
        write!(f, "{} at position {}", self.message, self.position)
    }
}

impl std::error::Error for ParseError {}

#[derive(Debug, Clone, PartialEq)]
enum TokenKind {
    /// Integer literal (no decimal point).
    Integer(i64),
    /// Decimal literal (contains a `.`). Non-negative and finite by construction.
    Decimal(f64),
    Ident(String),
    Plus,
    Minus,
    Star,
    Slash,
    Caret,
    Sqrt,
    Pi,
    LParen,
    RParen,
    Comma,
    Eof,
}

#[derive(Debug, Clone, PartialEq)]
struct Token {
    kind: TokenKind,
    /// 0-based char offset where this token starts.
    pos: usize,
}

struct Lexer {
    chars: Vec<char>,
    pos: usize,
}

impl Lexer {
    fn new(input: &str) -> Lexer {
        Lexer {
            chars: input.chars().collect(),
            pos: 0,
        }
    }

    fn current(&self) -> Option<char> {
        self.chars.get(self.pos).copied()
    }

    fn skip_whitespace(&mut self) {
        while matches!(self.current(), Some(c) if c.is_whitespace()) {
            self.pos += 1;
        }
    }

    /// Read a number: `digit+ ("." digit+)?`. The caller guarantees the first
    /// character is an ASCII digit.
    fn read_number(&mut self) -> Result<TokenKind, ParseError> {
        let start = self.pos;
        while matches!(self.current(), Some(c) if c.is_ascii_digit()) {
            self.pos += 1;
        }

        if self.current() == Some('.') {
            self.pos += 1;
            if !matches!(self.current(), Some(c) if c.is_ascii_digit()) {
                return Err(ParseError {
                    message: "expected digit after decimal point".to_string(),
                    position: self.pos - 1,
                });
            }
            while matches!(self.current(), Some(c) if c.is_ascii_digit()) {
                self.pos += 1;
            }
            let text: String = self.chars[start..self.pos].iter().collect();
            let value: f64 = text.parse().map_err(|_| ParseError {
                message: "decimal literal out of range".to_string(),
                position: start,
            })?;
            if !value.is_finite() {
                return Err(ParseError {
                    message: "decimal literal out of range".to_string(),
                    position: start,
                });
            }
            Ok(TokenKind::Decimal(value))
        } else {
            let text: String = self.chars[start..self.pos].iter().collect();
            let value: i64 = text.parse().map_err(|_| ParseError {
                message: "integer literal out of range".to_string(),
                position: start,
            })?;
            Ok(TokenKind::Integer(value))
        }
    }

    /// Read an ASCII identifier. The caller guarantees the first character is
    /// an ASCII letter or `_`.
    fn read_ident(&mut self) -> String {
        let start = self.pos;
        while matches!(self.current(), Some(c) if c.is_ascii_alphanumeric() || c == '_') {
            self.pos += 1;
        }
        self.chars[start..self.pos].iter().collect()
    }

    fn next_token(&mut self) -> Result<Token, ParseError> {
        self.skip_whitespace();
        let pos = self.pos;
        let kind = match self.current() {
            None => TokenKind::Eof,
            Some('+') => {
                self.pos += 1;
                TokenKind::Plus
            }
            Some('-') => {
                self.pos += 1;
                TokenKind::Minus
            }
            // MINUS SIGN U+2212
            Some('−') => {
                self.pos += 1;
                TokenKind::Minus
            }
            Some('*') => {
                self.pos += 1;
                TokenKind::Star
            }
            // MIDDLE DOT U+00B7, DOT OPERATOR U+22C5, MULTIPLICATION SIGN U+00D7
            Some('·') | Some('⋅') | Some('×') => {
                self.pos += 1;
                TokenKind::Star
            }
            Some('/') => {
                self.pos += 1;
                TokenKind::Slash
            }
            // DIVISION SIGN U+00F7
            Some('÷') => {
                self.pos += 1;
                TokenKind::Slash
            }
            Some('^') => {
                self.pos += 1;
                TokenKind::Caret
            }
            // SQUARE ROOT U+221A
            Some('√') => {
                self.pos += 1;
                TokenKind::Sqrt
            }
            // GREEK SMALL LETTER PI U+03C0
            Some('π') => {
                self.pos += 1;
                TokenKind::Pi
            }
            Some('(') => {
                self.pos += 1;
                TokenKind::LParen
            }
            Some(')') => {
                self.pos += 1;
                TokenKind::RParen
            }
            Some(',') => {
                self.pos += 1;
                TokenKind::Comma
            }
            Some(c) if c.is_ascii_digit() => self.read_number()?,
            Some(c) if c.is_ascii_alphabetic() || c == '_' => TokenKind::Ident(self.read_ident()),
            Some(c) => {
                return Err(ParseError {
                    message: format!("unexpected character '{}'", c),
                    position: pos,
                })
            }
        };
        Ok(Token { kind, pos })
    }
}

struct Parser {
    lexer: Lexer,
    current: Token,
    depth: usize,
}

impl Parser {
    fn new(input: &str) -> Result<Parser, ParseError> {
        let mut lexer = Lexer::new(input);
        let current = lexer.next_token()?;
        Ok(Parser {
            lexer,
            current,
            depth: 0,
        })
    }

    fn advance(&mut self) -> Result<(), ParseError> {
        self.current = self.lexer.next_token()?;
        Ok(())
    }

    /// Enter one recursion level of the descent. Every mutually recursive
    /// production (`expr` via parentheses, `unary` via operators and `^`'s
    /// right operand) goes through here, so all unbounded recursion is caught.
    fn enter(&mut self) -> Result<(), ParseError> {
        self.depth += 1;
        if self.depth > MAX_DEPTH {
            return Err(ParseError {
                message: format!("expression exceeds maximum nesting depth of {}", MAX_DEPTH),
                position: self.current.pos,
            });
        }
        Ok(())
    }

    fn parse_expr(&mut self) -> Result<Expr, ParseError> {
        self.enter()?;
        let result = self.parse_add();
        self.depth -= 1;
        result
    }

    fn parse_add(&mut self) -> Result<Expr, ParseError> {
        let mut left = self.parse_mul()?;
        loop {
            let op = match self.current.kind {
                TokenKind::Plus => BinOp::Add,
                TokenKind::Minus => BinOp::Sub,
                _ => break,
            };
            self.advance()?;
            let right = self.parse_mul()?;
            left = Expr::BinOp {
                op,
                left: Box::new(left),
                right: Box::new(right),
            };
        }
        Ok(left)
    }

    fn parse_mul(&mut self) -> Result<Expr, ParseError> {
        let mut left = self.parse_unary()?;
        loop {
            let op = match self.current.kind {
                TokenKind::Star => BinOp::Mul,
                TokenKind::Slash => BinOp::Div,
                _ => break,
            };
            self.advance()?;
            let right = self.parse_unary()?;
            left = Expr::BinOp {
                op,
                left: Box::new(left),
                right: Box::new(right),
            };
        }
        Ok(left)
    }

    fn parse_unary(&mut self) -> Result<Expr, ParseError> {
        self.enter()?;
        let result = match self.current.kind {
            TokenKind::Plus => {
                self.advance()?;
                let operand = self.parse_unary()?;
                Ok(Expr::UnaryOp {
                    op: UnaryOp::Pos,
                    operand: Box::new(operand),
                })
            }
            TokenKind::Minus => {
                self.advance()?;
                let operand = self.parse_unary()?;
                Ok(Expr::UnaryOp {
                    op: UnaryOp::Neg,
                    operand: Box::new(operand),
                })
            }
            TokenKind::Sqrt => {
                self.advance()?;
                let operand = self.parse_unary()?;
                Ok(Expr::Call {
                    func: Function::Sqrt,
                    args: vec![operand],
                })
            }
            _ => self.parse_pow(),
        };
        self.depth -= 1;
        result
    }

    fn parse_pow(&mut self) -> Result<Expr, ParseError> {
        let left = self.parse_primary()?;
        if self.current.kind == TokenKind::Caret {
            self.advance()?;
            // Right side is parsed at the `unary` level: this makes `^`
            // right-associative and allows `2^-3`.
            let right = self.parse_unary()?;
            return Ok(Expr::BinOp {
                op: BinOp::Pow,
                left: Box::new(left),
                right: Box::new(right),
            });
        }
        Ok(left)
    }

    fn parse_primary(&mut self) -> Result<Expr, ParseError> {
        let token = self.current.clone();
        match token.kind {
            TokenKind::Integer(v) => {
                self.advance()?;
                Ok(Expr::Integer(v))
            }
            TokenKind::Decimal(v) => {
                self.advance()?;
                Ok(Expr::Decimal(v))
            }
            TokenKind::Pi => {
                self.advance()?;
                Ok(Expr::Constant(Const::Pi))
            }
            TokenKind::Ident(name) => {
                if name == "pi" {
                    self.advance()?;
                    return Ok(Expr::Constant(Const::Pi));
                }
                if name == "e" {
                    self.advance()?;
                    return Ok(Expr::Constant(Const::E));
                }
                self.advance()?;
                if Function::from_name(&name).is_some() && self.current.kind != TokenKind::LParen {
                    // Function names are reserved: a bare `sin` is almost
                    // certainly a mistake, so fail specifically instead of
                    // silently treating it as a variable.
                    return Err(ParseError {
                        message: format!(
                            "function '{}' must be called with parentheses: {}(x)",
                            name, name
                        ),
                        position: token.pos,
                    });
                }
                if self.current.kind == TokenKind::LParen {
                    self.advance()?;
                    let func = Function::from_name(&name).ok_or_else(|| ParseError {
                        message: format!(
                            "unknown function '{}'; supported: sin, cos, tan, exp, ln, sqrt, abs",
                            name
                        ),
                        position: token.pos,
                    })?;
                    let arg = self.parse_single_arg(func)?;
                    return Ok(Expr::Call {
                        func,
                        args: vec![arg],
                    });
                }
                Ok(Expr::Variable(name))
            }
            TokenKind::LParen => {
                self.advance()?;
                let expr = self.parse_expr()?;
                if self.current.kind != TokenKind::RParen {
                    return Err(ParseError {
                        message: "expected ')'".to_string(),
                        position: self.current.pos,
                    });
                }
                self.advance()?;
                Ok(expr)
            }
            TokenKind::Eof => Err(ParseError {
                message: "expected a number, variable, constant, function or '('".to_string(),
                position: token.pos,
            }),
            _ => Err(ParseError {
                message: "expected a number, variable, constant, function or '('".to_string(),
                position: token.pos,
            }),
        }
    }

    /// Parse the single argument of a unary function and the closing paren.
    fn parse_single_arg(&mut self, func: Function) -> Result<Expr, ParseError> {
        if self.current.kind == TokenKind::RParen {
            return Err(ParseError {
                message: format!("{} expects exactly 1 argument, found 0", func.name()),
                position: self.current.pos,
            });
        }
        let arg = self.parse_expr()?;
        if self.current.kind == TokenKind::Comma {
            return Err(ParseError {
                message: format!("{} expects exactly 1 argument", func.name()),
                position: self.current.pos,
            });
        }
        if self.current.kind != TokenKind::RParen {
            return Err(ParseError {
                message: "expected ',' or ')'".to_string(),
                position: self.current.pos,
            });
        }
        self.advance()?;
        Ok(arg)
    }
}

/// Parse a mathematical expression into an AST.
pub fn parse(input: &str) -> Result<Expr, ParseError> {
    let mut parser = Parser::new(input)?;
    let expr = parser.parse_expr()?;
    if parser.current.kind != TokenKind::Eof {
        // Tokens that can start a primary almost always mean the writer
        // expected implicit multiplication ("2x", "2(x+1)", "2 3", "ππ").
        let implicit_hint = matches!(
            parser.current.kind,
            TokenKind::Ident(_)
                | TokenKind::Integer(_)
                | TokenKind::Decimal(_)
                | TokenKind::LParen
                | TokenKind::Pi
                | TokenKind::Sqrt
        );
        let message = if implicit_hint {
            "unexpected token after expression (implicit multiplication is not supported; insert '*')"
        } else {
            "unexpected token after expression"
        };
        return Err(ParseError {
            message: message.to_string(),
            position: parser.current.pos,
        });
    }
    Ok(expr)
}

/// Render an expression back to text in a canonical, unambiguous form that
/// `parse` maps back to the structurally identical AST. Binary operands are
/// parenthesised whenever they are themselves binary or unary operations, so
/// precedence and associativity can never be lost:
/// `Pow(Neg(x), 2)` prints as `(-x) ^ 2`, and `Pow(Pow(2, 3), 2)` prints as
/// `(2 ^ 3) ^ 2`.
pub fn print(expr: &Expr) -> String {
    let mut out = String::new();
    write_expr(&mut out, expr);
    out
}

fn write_expr(out: &mut String, expr: &Expr) {
    match expr {
        Expr::Integer(v) => out.push_str(&v.to_string()),
        Expr::Decimal(v) => {
            // Display never uses exponent notation, so a non-integral value
            // always contains '.'. Integral values need an explicit ".0" so
            // they re-parse as Decimal, not Integer.
            let text = v.to_string();
            out.push_str(&text);
            if !text.contains('.') {
                out.push_str(".0");
            }
        }
        Expr::Constant(c) => out.push_str(c.name()),
        Expr::Variable(name) => out.push_str(name),
        Expr::BinOp { op, left, right } => {
            write_operand(out, left);
            out.push(' ');
            out.push_str(op.symbol());
            out.push(' ');
            write_operand(out, right);
        }
        Expr::UnaryOp { op, operand } => {
            out.push_str(op.symbol());
            write_operand(out, operand);
        }
        Expr::Call { func, args } => {
            out.push_str(func.name());
            out.push('(');
            for (i, arg) in args.iter().enumerate() {
                if i > 0 {
                    out.push_str(", ");
                }
                write_expr(out, arg);
            }
            out.push(')');
        }
    }
}

/// Wrap an operand in parentheses unless it is a leaf (number, constant,
/// variable) or a function call — those bind tighter than every operator.
fn write_operand(out: &mut String, expr: &Expr) {
    let needs_parens = matches!(expr, Expr::BinOp { .. } | Expr::UnaryOp { .. });
    if needs_parens {
        out.push('(');
        write_expr(out, expr);
        out.push(')');
    } else {
        write_expr(out, expr);
    }
}

impl fmt::Display for Expr {
    fn fmt(&self, f: &mut fmt::Formatter) -> fmt::Result {
        f.write_str(&print(self))
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn ok(input: &str) -> Expr {
        parse(input).unwrap_or_else(|e| panic!("{:?} should parse, got {}", input, e))
    }

    // ---- precedence and associativity ----

    #[test]
    fn add_binds_looser_than_mul() {
        assert_eq!(
            ok("2 + 3 * 4"),
            Expr::BinOp {
                op: BinOp::Add,
                left: Box::new(Expr::Integer(2)),
                right: Box::new(Expr::BinOp {
                    op: BinOp::Mul,
                    left: Box::new(Expr::Integer(3)),
                    right: Box::new(Expr::Integer(4)),
                }),
            }
        );
        assert_eq!(ok("1 + 2 * 3 ^ 2"), ok("1 + (2 * (3 ^ 2))"));
    }

    #[test]
    fn pow_is_right_associative() {
        assert_eq!(ok("2^3^2"), ok("2^(3^2)"));
        assert_eq!(
            ok("2^3^2"),
            Expr::BinOp {
                op: BinOp::Pow,
                left: Box::new(Expr::Integer(2)),
                right: Box::new(ok("3^2")),
            }
        );
    }

    #[test]
    fn unary_minus_binds_looser_than_pow() {
        // -x^2 = -(x^2), standard algebraic convention. The pre-rewrite
        // parser implemented (-x)^2 while its comment claimed the opposite.
        assert_eq!(
            ok("-x^2"),
            Expr::UnaryOp {
                op: UnaryOp::Neg,
                operand: Box::new(ok("x^2")),
            }
        );
        // Explicit parentheses flip it, as they must.
        assert_eq!(
            ok("(-x)^2"),
            Expr::BinOp {
                op: BinOp::Pow,
                left: Box::new(ok("-x")),
                right: Box::new(Expr::Integer(2)),
            }
        );
    }

    #[test]
    fn pow_allows_unary_exponent() {
        assert_eq!(ok("2^-3"), ok("2^(-3)"));
    }

    #[test]
    fn add_sub_mul_div_are_left_associative() {
        assert_eq!(ok("8 / 4 / 2"), ok("(8 / 4) / 2"));
        assert_eq!(ok("10 - 2 - 3"), ok("(10 - 2) - 3"));
        assert_eq!(ok("1 + 2 + 3 + 4"), ok("(((1 + 2) + 3) + 4)"));
        assert_eq!(ok("2 * 3 * 4"), ok("(2 * 3) * 4"));
        assert_eq!(ok("6 / 2 * 3"), ok("(6 / 2) * 3"));
    }

    #[test]
    fn unary_operands_in_mul() {
        assert_eq!(ok("2 * -3"), ok("2 * (-3)"));
        assert_eq!(ok("1 - -x"), ok("1 - (-x)"));
    }

    // ---- literals, constants, variables ----

    #[test]
    fn integers_and_decimals() {
        assert_eq!(ok("42"), Expr::Integer(42));
        assert_eq!(ok("007"), Expr::Integer(7));
        assert_eq!(ok("3.75"), Expr::Decimal(3.75)); // not 3.14: clippy::approx_constant
        assert_eq!(ok("2.5"), Expr::Decimal(2.5));
        // Integral decimals print with a trailing ".0" so they re-parse as
        // Decimal rather than Integer.
        let p = ok("12.0");
        assert_eq!(p, Expr::Decimal(12.0));
        assert_eq!(print(&p), "12.0");
        assert_eq!(parse(&print(&p)).unwrap(), p);
    }

    #[test]
    fn constants() {
        assert_eq!(ok("pi"), Expr::Constant(Const::Pi));
        assert_eq!(ok("π"), Expr::Constant(Const::Pi));
        assert_eq!(ok("e"), Expr::Constant(Const::E));
        assert_eq!(ok("π * r ^ 2"), ok("pi * r ^ 2"));
    }

    #[test]
    fn variables() {
        assert_eq!(ok("x"), Expr::Variable("x".to_string()));
        assert_eq!(ok("y_2"), Expr::Variable("y_2".to_string()));
        assert_eq!(ok("_priv"), Expr::Variable("_priv".to_string()));
        assert_eq!(ok("alpha"), Expr::Variable("alpha".to_string()));
    }

    // ---- unicode forms ----

    #[test]
    fn unicode_operators() {
        assert_eq!(ok("2 · 3"), ok("2 * 3")); // MIDDLE DOT
        assert_eq!(ok("2⋅3"), ok("2 * 3")); // DOT OPERATOR
        assert_eq!(ok("2 × 3"), ok("2 * 3")); // MULTIPLICATION SIGN
        assert_eq!(ok("6 ÷ 2"), ok("6 / 2")); // DIVISION SIGN
        assert_eq!(ok("−x + 1"), ok("-x + 1")); // MINUS SIGN U+2212
        assert_eq!(ok("√x"), ok("sqrt(x)"));
        assert_eq!(ok("√(x + 1)"), ok("sqrt(x + 1)"));
        // √ binds exactly like unary minus: √x^2 = √(x^2).
        assert_eq!(ok("√x ^ 2"), ok("sqrt(x ^ 2)"));
    }

    // ---- functions ----

    #[test]
    fn all_supported_functions_parse() {
        for name in ["sin", "cos", "tan", "exp", "ln", "sqrt", "abs"] {
            let e = ok(&format!("{}(x)", name));
            match e {
                Expr::Call { func, args } => {
                    assert_eq!(func.name(), name);
                    assert_eq!(args.len(), 1);
                }
                other => panic!("{}(x) parsed as {:?}, expected a call", name, other),
            }
        }
    }

    #[test]
    fn functions_nest() {
        // sin(x)^2 must be a power whose left operand is the call — not
        // sin(x^2) and not (sin^2)(x).
        assert_eq!(ok("sin(x)^2"), ok("(sin(x))^2"));
        match ok("sin(cos(tan(x)))") {
            Expr::Call { func, args } => {
                assert_eq!(func, Function::Sin);
                assert_eq!(args.len(), 1);
                match &args[0] {
                    Expr::Call { func, args } => {
                        assert_eq!(func, &Function::Cos);
                        assert_eq!(args.len(), 1);
                        match &args[0] {
                            Expr::Call { func, args } => {
                                assert_eq!(func, &Function::Tan);
                                assert_eq!(args.len(), 1);
                                assert_eq!(args[0], Expr::Variable("x".to_string()));
                            }
                            other => panic!("innermost should be tan(x), got {:?}", other),
                        }
                    }
                    other => panic!("middle should be cos(...), got {:?}", other),
                }
            }
            other => panic!("outermost should be sin(...), got {:?}", other),
        }
    }

    // ---- canonical printing ----

    #[test]
    fn print_forms_are_canonical_and_unambiguous() {
        assert_eq!(print(&ok("2 + 3 * 4")), "(2 * 3) + 4");
        assert_eq!(print(&ok("2 ^ 3 ^ 2")), "2 ^ (3 ^ 2)");
        assert_eq!(print(&ok("-x^2")), "-(x ^ 2)");
        assert_eq!(print(&ok("(-x)^2")), "(-x) ^ 2");
        assert_eq!(print(&ok("2^-3")), "2 ^ (-3)");
        assert_eq!(print(&ok("sin(x + y)")), "sin(x + y)");
        assert_eq!(print(&ok("√(x + 1)")), "sqrt(x + 1)");
        assert_eq!(print(&ok("1 / 3")), "1 / 3");
        assert_eq!(print(&ok("2 * -3")), "2 * (-3)");
        assert_eq!(print(&ok("1 - (2 - 3)")), "1 - (2 - 3)");
        assert_eq!(print(&ok("(2 ^ 3) ^ 2")), "(2 ^ 3) ^ 2");
        assert_eq!(format!("{}", ok("x + 1")), "x + 1");
    }

    // ---- hand-written corpora ----

    /// Valid inputs; each must parse and survive `parse(print(parse(s)))`
    /// with a structurally identical AST.
    fn valid_corpus() -> Vec<&'static str> {
        vec![
            "0",
            "42",
            "007",
            "3.14",
            "2.5",
            "0.125",
            "100.0",
            "12.0",
            "x",
            "y_2",
            "_priv",
            "alpha",
            "pi",
            "e",
            "π",
            "x + y",
            "x - y",
            "x * y",
            "x / y",
            "x ^ y",
            "2 + 3 * 4",
            "(2 + 3) * 4",
            "2 ^ 3 ^ 2",
            "(2 ^ 3) ^ 2",
            "-x",
            "+x",
            "-x^2",
            "(-x)^2",
            "2^-3",
            "-2^2",
            "1 / 3",
            "3/4",
            "sin(x)",
            "cos(x + 1)",
            "tan(2*x)",
            "exp(x)",
            "ln(x) + 1",
            "sqrt(x^2 + 1)",
            "abs(-x)",
            "√x",
            "√(x + 1)",
            "√x^2",
            "√π",
            "√(2·3)",
            "π * r ^ 2",
            "2 · 3",
            "2⋅3",
            "2 × 3",
            "6 ÷ 2",
            "−x + 1",
            "sin(cos(tan(x)))",
            "x + y * z ^ 2 - 1",
            "((((x))))",
            "1 + 2 + 3 + 4",
            "10 / 5 / 2",
            "10 - 2 - 3",
            "1 - -x",
            "2 * -3",
        ]
    }

    /// Malformed inputs (work order B1 requires >= 40), each with the specific
    /// error substring it must produce. None may panic.
    fn malformed_corpus() -> Vec<(&'static str, &'static str)> {
        vec![
            // empty / whitespace-only
            ("", "expected a number"),
            ("   ", "expected a number"),
            ("\t\n ", "expected a number"),
            // bad characters
            ("2 @", "unexpected character '@'"),
            ("x # y", "unexpected character '#'"),
            ("a $ b", "unexpected character '$'"),
            ("1 ! 2", "unexpected character '!'"),
            ("sin(x)?", "unexpected character '?'"),
            ("2;3", "unexpected character ';'"),
            ("[1]", "unexpected character '['"),
            ("{x}", "unexpected character '{'"),
            ("α + 1", "unexpected character 'α'"),
            // malformed numbers
            ("1.", "expected digit after decimal point"),
            ("1.2.3", "unexpected character '.'"),
            (".5", "unexpected character '.'"),
            ("1..2", "expected digit after decimal point"),
            ("1 . 5", "unexpected character '.'"),
            ("99999999999999999999999", "integer literal out of range"),
            ("1e5", "insert '*'"), // no exponent notation: 'e' is a constant
            // parens / call structure
            ("(2 + 3", "expected ')'"),
            ("2 + 3)", "unexpected token after expression"),
            ("()", "expected a number"),
            ("(,)", "expected a number"),
            ("(2 + 3))", "unexpected token after expression"),
            ("((2)", "expected ')'"),
            ("sin(x", "expected ',' or ')'"),
            // dangling operators
            ("+", "expected a number"),
            ("-", "expected a number"),
            ("2 +", "expected a number"),
            ("* 2", "expected a number"),
            ("2 *", "expected a number"),
            ("2 /", "expected a number"),
            ("2 ^", "expected a number"),
            ("2 ** 3", "expected a number"),
            ("2 ^ ^ 3", "expected a number"),
            ("√", "expected a number"),
            ("√ * 2", "expected a number"),
            // function names and arity
            ("sin", "function 'sin' must be called"),
            ("sin * 2", "function 'sin' must be called"),
            ("foo(x)", "unknown function 'foo'"),
            ("log(x)", "unknown function 'log'"),
            ("sin()", "sin expects exactly 1 argument, found 0"),
            ("sin(x, y)", "sin expects exactly 1 argument"),
            ("abs(x, 2)", "abs expects exactly 1 argument"),
            // trailing tokens / implicit multiplication
            ("2 3", "insert '*'"),
            ("x y", "insert '*'"),
            ("2(x)", "insert '*'"),
            ("pi(2)", "insert '*'"),
            ("2e", "insert '*'"),
            ("ππ", "insert '*'"),
        ]
    }

    #[test]
    fn malformed_corpus_produces_specific_errors() {
        let corpus = malformed_corpus();
        assert!(
            corpus.len() >= 40,
            "corpus has {} entries, need >= 40",
            corpus.len()
        );
        for (input, expected) in &corpus {
            match parse(input) {
                Ok(e) => panic!("malformed input {:?} parsed as {:?}", input, e),
                Err(err) => assert!(
                    err.message.contains(expected),
                    "input {:?}: expected message containing {:?}, got {:?}",
                    input,
                    expected,
                    err.message
                ),
            }
        }
    }

    #[test]
    fn valid_corpus_round_trips() {
        for input in valid_corpus() {
            let first = parse(input).unwrap_or_else(|e| panic!("{:?} should parse: {}", input, e));
            let printed = print(&first);
            let second = parse(&printed).unwrap_or_else(|e| {
                panic!(
                    "{:?} printed as {:?} failed to re-parse: {}",
                    input, printed, e
                )
            });
            assert_eq!(
                second, first,
                "round-trip changed {:?} (printed as {:?})",
                input, printed
            );
            // Spec form: parse(print(parse(s))) == parse(s).
            let third = parse(&print(&second)).unwrap();
            assert_eq!(third, first, "double round-trip changed {:?}", input);
        }
    }

    // ---- errors: positions, display, depth ----

    #[test]
    fn error_positions_are_character_offsets() {
        let e = parse("2 @").unwrap_err();
        assert_eq!(e.message, "unexpected character '@'");
        assert_eq!(e.position, 2);

        assert_eq!(parse("1.").unwrap_err().position, 1);
        assert_eq!(parse(".5").unwrap_err().position, 0);
        assert_eq!(parse("  2 +").unwrap_err().position, 5); // EOF
        assert_eq!(parse("(2 + 3").unwrap_err().position, 6); // EOF
        assert_eq!(parse("sin(x, y)").unwrap_err().position, 5); // the comma
    }

    #[test]
    fn parse_error_display() {
        let e = parse("2 @").unwrap_err();
        assert_eq!(format!("{}", e), "unexpected character '@' at position 2");
    }

    #[test]
    fn deep_nesting_is_an_error_not_a_crash() {
        // ~2 recursion levels per paren pair, so 600 pairs is far past the
        // 500-level cap; the point is that it returns an error at all.
        let deep_parens = format!("{}x{}", "(".repeat(600), ")".repeat(600));
        let e = parse(&deep_parens).unwrap_err();
        assert!(e.message.contains("nesting depth"), "got: {}", e.message);

        let long_negation = format!("{}x", "-".repeat(600));
        let e = parse(&long_negation).unwrap_err();
        assert!(e.message.contains("nesting depth"), "got: {}", e.message);

        // Below the cap still works.
        let ok_parens = format!("{}x{}", "(".repeat(200), ")".repeat(200));
        assert!(parse(&ok_parens).is_ok());
        let ok_negation = format!("{}x", "-".repeat(200));
        assert!(parse(&ok_negation).is_ok());
    }
}
