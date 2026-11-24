fn main() {
    println!("hello from nucleus linux abi");
    let args: Vec<String> = std::env::args().collect();
    for (i, arg) in args.iter().enumerate() {
        println!("argv[{}]: {}", i, arg);
    }
}
