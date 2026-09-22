//! Expression parser: text → AST.
//!
//! Parses mathematical expressions into an abstract syntax tree. Handles:
//! - Numbers (integers, floats)
//! - Variables (single letters or multi-character identifiers)
//! - Binary operators: +, -, *, /, ^ (exponentiation)
//! - Unary operators: -, +
//! - Parentheses for grouping
//! - Function calls: sin, cos, sqrt, etc.
//!
//! Operator precedence (highest to lowest):
//! 1. Function application, parentheses
//! 2. Exponentiation (right-associative)
//! 3. Unary +/-
//! 4. Multiplication, division
//! 5. Addition, subtraction

use std::fmt;

#[derive(Debug, Clone, PartialEq)]
pub enum Expr {
    Number(f64),
    Variable(String),
    BinOp {
        op: BinOp,
        left: Box<Expr>,
        right: Box<Expr>,
    },
    UnaryOp {
        op: UnaryOp,
        operand: Box<Expr>,
    },
    Call {
        func: String,
        args: Vec<Expr>,
    },
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum BinOp {
    Add,
    Sub,
    Mul,
    Div,
    Pow,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum UnaryOp {
    Neg,
    Pos,
}

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

struct Lexer {
    input: Vec<char>,
    pos: usize,
}

#[derive(Debug, Clone, PartialEq)]
enum Token {
    Number(f64),
    Ident(String),
    Plus,
    Minus,
    Star,
    Slash,
    Caret,
    LParen,
    RParen,
    Comma,
    Eof,
}

impl Lexer {
    fn new(input: &str) -> Self {
        Lexer {
            input: input.chars().collect(),
            pos: 0,
        }
    }

    fn current(&self) -> Option<char> {
        if self.pos < self.input.len() {
            Some(self.input[self.pos])
        } else {
            None
        }
    }

    fn advance(&mut self) {
        self.pos += 1;
    }

    fn skip_whitespace(&mut self) {
        while let Some(ch) = self.current() {
            if ch.is_whitespace() {
                self.advance();
            } else {
                break;
            }
        }
    }

    fn read_number(&mut self) -> Result<f64, ParseError> {
        let start = self.pos;
        while let Some(ch) = self.current() {
            if ch.is_ascii_digit() || ch == '.' {
                self.advance();
            } else {
                break;
            }
        }
        let num_str: String = self.input[start..self.pos].iter().collect();
        num_str.parse::<f64>().map_err(|_| ParseError {
            message: "invalid number".to_string(),
            position: start,
        })
    }

    fn read_ident(&mut self) -> String {
        let start = self.pos;
        while let Some(ch) = self.current() {
            if ch.is_alphanumeric() || ch == '_' {
                self.advance();
            } else {
                break;
            }
        }
        self.input[start..self.pos].iter().collect()
    }

    fn next_token(&mut self) -> Result<Token, ParseError> {
        self.skip_whitespace();

        match self.current() {
            None => Ok(Token::Eof),
            Some('+') => {
                self.advance();
                Ok(Token::Plus)
            }
            Some('-') => {
                self.advance();
                Ok(Token::Minus)
            }
            Some('*') => {
                self.advance();
                Ok(Token::Star)
            }
            Some('/') => {
                self.advance();
                Ok(Token::Slash)
            }
            Some('^') => {
                self.advance();
                Ok(Token::Caret)
            }
            Some('(') => {
                self.advance();
                Ok(Token::LParen)
            }
            Some(')') => {
                self.advance();
                Ok(Token::RParen)
            }
            Some(',') => {
                self.advance();
                Ok(Token::Comma)
            }
            Some(ch) if ch.is_ascii_digit() => Ok(Token::Number(self.read_number()?)),
            Some(ch) if ch.is_alphabetic() || ch == '_' => Ok(Token::Ident(self.read_ident())),
            Some(ch) => Err(ParseError {
                message: format!("unexpected character: '{}'", ch),
                position: self.pos,
            }),
        }
    }
}

pub struct Parser {
    lexer: Lexer,
    current: Token,
}

impl Parser {
    fn new(input: &str) -> Result<Self, ParseError> {
        let mut lexer = Lexer::new(input);
        let current = lexer.next_token()?;
        Ok(Parser { lexer, current })
    }

    fn advance(&mut self) -> Result<(), ParseError> {
        self.current = self.lexer.next_token()?;
        Ok(())
    }

    fn peek(&self) -> &Token {
        &self.current
    }

    fn parse_expr(&mut self) -> Result<Expr, ParseError> {
        self.parse_add_sub()
    }

    fn parse_add_sub(&mut self) -> Result<Expr, ParseError> {
        let mut left = self.parse_mul_div()?;

        loop {
            match self.peek() {
                Token::Plus => {
                    self.advance()?;
                    let right = self.parse_mul_div()?;
                    left = Expr::BinOp {
                        op: BinOp::Add,
                        left: Box::new(left),
                        right: Box::new(right),
                    };
                }
                Token::Minus => {
                    self.advance()?;
                    let right = self.parse_mul_div()?;
                    left = Expr::BinOp {
                        op: BinOp::Sub,
                        left: Box::new(left),
                        right: Box::new(right),
                    };
                }
                _ => break,
            }
        }

        Ok(left)
    }

    fn parse_mul_div(&mut self) -> Result<Expr, ParseError> {
        let mut left = self.parse_pow()?;

        loop {
            match self.peek() {
                Token::Star => {
                    self.advance()?;
                    let right = self.parse_pow()?;
                    left = Expr::BinOp {
                        op: BinOp::Mul,
                        left: Box::new(left),
                        right: Box::new(right),
                    };
                }
                Token::Slash => {
                    self.advance()?;
                    let right = self.parse_pow()?;
                    left = Expr::BinOp {
                        op: BinOp::Div,
                        left: Box::new(left),
                        right: Box::new(right),
                    };
                }
                _ => break,
            }
        }

        Ok(left)
    }

    fn parse_pow(&mut self) -> Result<Expr, ParseError> {
        let mut left = self.parse_unary()?;

        if matches!(self.peek(), Token::Caret) {
            self.advance()?;
            let right = self.parse_pow()?; // Right-associative
            left = Expr::BinOp {
                op: BinOp::Pow,
                left: Box::new(left),
                right: Box::new(right),
            };
        }

        Ok(left)
    }

    fn parse_unary(&mut self) -> Result<Expr, ParseError> {
        match self.peek() {
            Token::Plus => {
                self.advance()?;
                let operand = self.parse_unary()?;
                Ok(Expr::UnaryOp {
                    op: UnaryOp::Pos,
                    operand: Box::new(operand),
                })
            }
            Token::Minus => {
                self.advance()?;
                let operand = self.parse_unary()?;
                Ok(Expr::UnaryOp {
                    op: UnaryOp::Neg,
                    operand: Box::new(operand),
                })
            }
            _ => self.parse_primary(),
        }
    }

    fn parse_primary(&mut self) -> Result<Expr, ParseError> {
        match self.peek().clone() {
            Token::Number(n) => {
                self.advance()?;
                Ok(Expr::Number(n))
            }
            Token::Ident(name) => {
                self.advance()?;
                match self.peek() {
                    Token::LParen => {
                        // Function call
                        self.advance()?;
                        let args = self.parse_args()?;
                        Ok(Expr::Call { func: name, args })
                    }
                    _ => {
                        // Variable
                        Ok(Expr::Variable(name))
                    }
                }
            }
            Token::LParen => {
                self.advance()?;
                let expr = self.parse_expr()?;
                if !matches!(self.peek(), Token::RParen) {
                    return Err(ParseError {
                        message: "expected ')'".to_string(),
                        position: self.lexer.pos,
                    });
                }
                self.advance()?;
                Ok(expr)
            }
            _ => Err(ParseError {
                message: "expected number, variable, or '('".to_string(),
                position: self.lexer.pos,
            }),
        }
    }

    fn parse_args(&mut self) -> Result<Vec<Expr>, ParseError> {
        let mut args = Vec::new();

        if matches!(self.peek(), Token::RParen) {
            self.advance()?;
            return Ok(args);
        }

        loop {
            args.push(self.parse_expr()?);
            match self.peek() {
                Token::Comma => {
                    self.advance()?;
                }
                Token::RParen => {
                    self.advance()?;
                    break;
                }
                _ => {
                    return Err(ParseError {
                        message: "expected ',' or ')'".to_string(),
                        position: self.lexer.pos,
                    })
                }
            }
        }

        Ok(args)
    }
}

/// Parse a mathematical expression into an AST.
pub fn parse(input: &str) -> Result<Expr, ParseError> {
    let mut parser = Parser::new(input)?;
    let expr = parser.parse_expr()?;
    if !matches!(parser.peek(), Token::Eof) {
        return Err(ParseError {
            message: "unexpected token after expression".to_string(),
            position: parser.lexer.pos,
        });
    }
    Ok(expr)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parse_number() {
        assert_eq!(parse("42"), Ok(Expr::Number(42.0)));
        assert_eq!(parse("3.14"), Ok(Expr::Number(3.14)));
    }

    #[test]
    fn parse_variable() {
        assert_eq!(parse("x"), Ok(Expr::Variable("x".to_string())));
        assert_eq!(parse("foo"), Ok(Expr::Variable("foo".to_string())));
    }

    #[test]
    fn parse_addition() {
        assert!(matches!(
            parse("2 + 3"),
            Ok(Expr::BinOp {
                op: BinOp::Add,
                ..
            })
        ));
    }

    #[test]
    fn parse_precedence() {
        // 2 + 3 * 4 should parse as 2 + (3 * 4), not (2 + 3) * 4
        let expr = parse("2 + 3 * 4").unwrap();
        if let Expr::BinOp {
            op: BinOp::Add,
            left,
            right,
        } = expr
        {
            assert!(matches!(**left, Expr::Number(2.0)));
            assert!(matches!(**right, Expr::BinOp { op: BinOp::Mul, .. }));
        } else {
            panic!("unexpected structure");
        }
    }

    #[test]
    fn parse_function_call() {
        assert!(matches!(
            parse("sin(x)"),
            Ok(Expr::Call {
                func,
                args
            }) if func == "sin" && args.len() == 1
        ));
    }

    #[test]
    fn parse_parentheses() {
        let expr = parse("(2 + 3) * 4").unwrap();
        if let Expr::BinOp {
            op: BinOp::Mul,
            left,
            right,
        } = expr
        {
            assert!(matches!(**left, Expr::BinOp { op: BinOp::Add, .. }));
            assert!(matches!(**right, Expr::Number(4.0)));
        } else {
            panic!("unexpected structure");
        }
    }

    #[test]
    fn parse_error() {
        assert!(parse("2 +").is_err());
        assert!(parse("(2 + 3").is_err());
    }
}
