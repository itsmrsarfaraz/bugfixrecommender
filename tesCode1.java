public boolean isAdminUser(User user) {
    // BUG: If user is null, user.getRole() will throw a NullPointerException immediately
    String role = user.getRole(); 
    
    if (user != null && role.equalsIgnoreCase("ADMIN")) {
        return true;
    }
    return false;
}